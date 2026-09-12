from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
import math
import socket
from typing import Literal, cast
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pydantic import ValidationError

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.merge_train_policy import (
    ProviderCodeScanningToolExpectationV1,
    ProviderDeliveryProtectionExpectationV1,
    ProviderPullRequestExpectationV1,
    ProviderRequiredStatusCheckExpectationV1,
    MergeTrainMergeMethod,
)
from control_plane.contracts.provider_delivery_inspection import (
    ProviderDeliveryInspectionFactsV1,
    ProviderDeliveryInspectionReason,
    ProviderDeliveryInspectionResultV1,
)
from control_plane.contracts.provider_delivery_readiness import (
    PROVIDER_INSPECTION_MAX_RETRY_AFTER_SECONDS,
)
from control_plane.github_app_identity import (
    GitHubApiRequest,
    GitHubAppIdentityError,
    GitHubAppInstallationToken,
    mint_provider_delivery_inspection_token,
    revoke_installation_token,
)
from control_plane.ordinary_agent_provider_wait import (
    provider_error_is_quota_limited,
    provider_wait_observation_from_exception,
)
from control_plane.provider_delivery_inspection_profile import (
    ResolvedProviderDeliveryInspectionProfile,
)


ProviderDeliveryInspectionCapabilityReason = Literal[
    "provider_wait",
    "provider_attempt_deadline",
    "provider_authentication_failed",
    "provider_permission_denied",
    "provider_transport",
    "cleanup_unknown",
]
ProviderDeliveryInspectionCleanupOutcome = Literal[
    "confirmed_revoked",
    "cleanup_unknown",
]
ProviderDeliveryInspectionApiRequest = Callable[..., object]
RemainingSeconds = Callable[[], float]
BeforeTokenMint = Callable[[int, int], None]
TokenIssued = Callable[[GitHubAppInstallationToken], None]
TokenCleanup = Callable[[ProviderDeliveryInspectionCleanupOutcome], None]

_MAX_PER_REQUEST_SECONDS = 15.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_RULESETS = 20
_RULE_PAGE_SIZE = 100
_KNOWN_RULE_TYPES = {
    "update",
    "deletion",
    "non_fast_forward",
    "required_status_checks",
    "code_scanning",
    "pull_request",
}


class ProviderDeliveryInspectionCapabilityError(RuntimeError):
    def __init__(
        self,
        reason_code: ProviderDeliveryInspectionCapabilityReason,
        *,
        retry_not_before: int | None = None,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retry_not_before = retry_not_before


class _SemanticInconclusive(ValueError):
    def __init__(self, reason_code: ProviderDeliveryInspectionReason) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _ProtectionNotReady(ValueError):
    def __init__(self, reason_code: ProviderDeliveryInspectionReason) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _ProviderResponseTooLarge(ValueError):
    pass


class _ProviderResponseMalformed(ValueError):
    pass


def inspect_provider_delivery_protection(
    *,
    profile: ResolvedProviderDeliveryInspectionProfile,
    repository: str,
    repository_id: int,
    repository_owner_id: int,
    base_branch: str,
    ordinary_delivery_app_id: int,
    expectation: ProviderDeliveryProtectionExpectationV1,
    remaining_provider_seconds: RemainingSeconds,
    remaining_cleanup_seconds: RemainingSeconds,
    before_token_mint: BeforeTokenMint,
    token_issued: TokenIssued,
    token_cleanup: TokenCleanup,
    api_request: ProviderDeliveryInspectionApiRequest | None = None,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ProviderDeliveryInspectionResultV1:
    """Inspect one exact base branch with a short-lived, separately governed App token."""
    normalized_repository, owner, repo = _normalize_target(
        repository=repository,
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        base_branch=base_branch,
        ordinary_delivery_app_id=ordinary_delivery_app_id,
    )
    request: ProviderDeliveryInspectionApiRequest = (
        api_request or _provider_delivery_inspection_api_request
    )
    request_count = 0
    cleanup_outcome: ProviderDeliveryInspectionCleanupOutcome | None = None
    cleanup_retry_not_before: int | None = None
    mint_marked = False

    def provider_request(**kwargs: object) -> object:
        nonlocal mint_marked, request_count
        timeout = _remaining_timeout(remaining_provider_seconds)
        path = kwargs.get("path")
        method = kwargs.get("method", "GET")
        if (
            method == "POST"
            and isinstance(path, str)
            and path.startswith("/app/installations/")
            and path.endswith("/access_tokens")
        ):
            installation_text = path.removeprefix("/app/installations/").removesuffix(
                "/access_tokens"
            )
            if mint_marked or not installation_text.isdecimal():
                raise ProviderDeliveryInspectionCapabilityError("provider_transport")
            # This marker is intentionally after the final local deadline check
            # and immediately before the transport. A crash or transport error
            # after it is an honestly unknown token-mint outcome.
            before_token_mint(profile.app_id, int(installation_text))
            mint_marked = True
        request_count += 1
        try:
            return request(**kwargs, timeout_seconds=timeout)
        except Exception as error:
            raise _capability_error(error, utc_now=utc_now) from error

    def cleanup_request(**kwargs: object) -> object:
        nonlocal cleanup_outcome, cleanup_retry_not_before, request_count
        timeout = _remaining_timeout(remaining_cleanup_seconds)
        request_count += 1
        try:
            response = request(**kwargs, timeout_seconds=timeout)
        except BaseException as error:
            if isinstance(error, Exception):
                cleanup_retry_not_before = _strongest_retry_not_before(
                    cleanup_retry_not_before,
                    _capability_error(error, utc_now=utc_now).retry_not_before,
                )
            cleanup_outcome = "cleanup_unknown"
            token_cleanup(cleanup_outcome)
            raise
        cleanup_outcome = "confirmed_revoked"
        token_cleanup(cleanup_outcome)
        return response

    installation_token: GitHubAppInstallationToken | None = None
    raw_observations: list[object] = []
    result: ProviderDeliveryInspectionResultV1 | None = None
    original_error: BaseException | None = None
    try:
        installation_token = mint_provider_delivery_inspection_token(
            identity=profile.identity,
            repository=normalized_repository,
            repository_id=str(repository_id),
            repository_owner_id=str(repository_owner_id),
            api_request=provider_request,
            now=utc_now(),
            validation_error_revoke_api_request=cleanup_request,
        )
        token_issued(installation_token)
        _remaining_timeout(remaining_provider_seconds)
        reader = _InspectionReader(
            token=installation_token.token,
            repository=normalized_repository,
            owner=owner,
            repo=repo,
            repository_id=repository_id,
            repository_owner_id=repository_owner_id,
            base_branch=base_branch.strip(),
            request=provider_request,
            raw_observations=raw_observations,
        )
        result = reader.inspect(
            ordinary_delivery_app_id=ordinary_delivery_app_id,
            expectation=expectation,
        )
        _remaining_timeout(remaining_provider_seconds)
    except BaseException as error:
        if isinstance(error, GitHubAppIdentityError):
            original_error = _identity_capability_error(error)
        else:
            original_error = error
    finally:
        if installation_token is not None:
            try:
                revoke_installation_token(
                    installation_token=installation_token,
                    api_request=cleanup_request,
                )
            except BaseException as cleanup_error:
                if cleanup_outcome is None:
                    token_cleanup("cleanup_unknown")
                capability_error = ProviderDeliveryInspectionCapabilityError(
                    "cleanup_unknown",
                    retry_not_before=_strongest_retry_not_before(
                        _error_retry_not_before(original_error),
                        cleanup_retry_not_before,
                    ),
                )
                if original_error is not None:
                    capability_error.add_note(
                        f"Inspection also failed: {type(original_error).__name__}"
                    )
                raise capability_error from cleanup_error
            if cleanup_outcome is None:
                token_cleanup("confirmed_revoked")
    if cleanup_outcome == "cleanup_unknown":
        capability_error = ProviderDeliveryInspectionCapabilityError(
            "cleanup_unknown",
            retry_not_before=_strongest_retry_not_before(
                _error_retry_not_before(original_error),
                cleanup_retry_not_before,
            ),
        )
        if original_error is not None:
            capability_error.add_note(f"Inspection also failed: {type(original_error).__name__}")
        raise capability_error from original_error
    if original_error is not None:
        raise original_error
    if result is None:
        raise RuntimeError("Provider-delivery inspection produced no result.")
    # The reader excludes JWT, installation token, token response, and secret data.
    digest = canonical_json_sha256(raw_observations)
    facts = result.facts
    if facts is not None:
        facts = facts.model_copy(
            update={
                "raw_observation_sha256": digest,
                "provider_request_count": request_count,
            }
        )
    return result.model_copy(
        update={
            "facts": facts,
            "raw_observation_sha256": digest,
            "provider_request_count": request_count,
        }
    )


class _InspectionReader:
    def __init__(
        self,
        *,
        token: str,
        repository: str,
        owner: str,
        repo: str,
        repository_id: int,
        repository_owner_id: int,
        base_branch: str,
        request: GitHubApiRequest,
        raw_observations: list[object],
    ) -> None:
        self._token = token
        self._repository = repository
        self._owner = owner
        self._repo = repo
        self._repository_id = repository_id
        self._repository_owner_id = repository_owner_id
        self._base_branch = base_branch
        self._request = request
        self._raw_observations = raw_observations

    def inspect(
        self,
        *,
        ordinary_delivery_app_id: int,
        expectation: ProviderDeliveryProtectionExpectationV1,
    ) -> ProviderDeliveryInspectionResultV1:
        try:
            repository_payload = self._get_json(self._repository_path())
            self._require_repository_identity(repository_payload)
            branch_payload = self._get_json(
                f"{self._repository_path()}/branches/{quote(self._base_branch, safe='')}"
            )
            self._require_branch_identity(branch_payload)
            if branch_payload.get("protected") is not True:
                return self._result("protection_not_ready", ("branch_not_protected",))

            evaluated = self._get_list(
                f"{self._repository_path()}/rules/branches/"
                f"{quote(self._base_branch, safe='')}?per_page={_RULE_PAGE_SIZE}"
            )
            if len(evaluated) == _RULE_PAGE_SIZE:
                raise _SemanticInconclusive("ruleset_page_full")
            ruleset_ids = self._evaluated_ruleset_ids(evaluated)
            if len(ruleset_ids) > _MAX_RULESETS:
                raise _SemanticInconclusive("ruleset_limit_exceeded")
            rulesets = tuple(self._read_ruleset(ruleset_id) for ruleset_id in ruleset_ids)
            classic = self._read_classic_protection(repository_payload)
            normalized = _normalize_protection(
                rulesets=rulesets,
                classic=classic,
                repository_payload=repository_payload,
                repository=self._repository,
                owner=self._owner,
                ruleset_ids=ruleset_ids,
                ordinary_delivery_app_id=ordinary_delivery_app_id,
            )
            reasons = _readiness_reasons(normalized.effective, expectation)
            status: Literal["ready", "protection_not_ready"] = (
                "ready" if not reasons else "protection_not_ready"
            )
            reason_codes: tuple[ProviderDeliveryInspectionReason, ...] = (
                ("provider_protection_ready",) if status == "ready" else reasons
            )
            placeholder_digest = "0" * 64
            facts = ProviderDeliveryInspectionFactsV1(
                repository_id=self._repository_id,
                repository_owner_id=self._repository_owner_id,
                repository=self._repository,
                base_branch=self._base_branch,
                ordinary_delivery_app_id=ordinary_delivery_app_id,
                applicable_ruleset_ids=ruleset_ids,
                update_ruleset_id=normalized.update_ruleset_id,
                classic_protection_present=classic is not None,
                effective_protection=normalized.effective,
                raw_observation_sha256=placeholder_digest,
                provider_request_count=1,
            )
            return ProviderDeliveryInspectionResultV1(
                status=status,
                reason_codes=reason_codes,
                facts=facts,
                raw_observation_sha256=placeholder_digest,
                provider_request_count=1,
            )
        except _SemanticInconclusive as error:
            return self._result("semantic_inconclusive", (error.reason_code,))
        except _ProtectionNotReady as error:
            return self._result("protection_not_ready", (error.reason_code,))
        except (KeyError, TypeError, ValueError, ValidationError):
            return self._result("semantic_inconclusive", ("provider_response_malformed",))

    def _result(
        self,
        status: Literal["protection_not_ready", "semantic_inconclusive"],
        reasons: tuple[ProviderDeliveryInspectionReason, ...],
    ) -> ProviderDeliveryInspectionResultV1:
        return ProviderDeliveryInspectionResultV1(
            status=status,
            reason_codes=reasons,
            raw_observation_sha256="0" * 64,
            provider_request_count=1,
        )

    def _repository_path(self) -> str:
        return f"/repos/{quote(self._owner, safe='')}/{quote(self._repo, safe='')}"

    def _get(self, path: str) -> object:
        if not _is_allowed_business_get(
            path=path,
            repository_path=self._repository_path(),
            base_branch=self._base_branch,
        ):
            raise _SemanticInconclusive("provider_visibility_incomplete")
        try:
            payload = self._request(path=path, token=self._token)
        except ProviderDeliveryInspectionCapabilityError as error:
            if _caused_by_type(error, _ProviderResponseTooLarge):
                raise _SemanticInconclusive("provider_visibility_incomplete") from error
            if _caused_by_type(error, _ProviderResponseMalformed):
                raise _SemanticInconclusive("provider_response_malformed") from error
            raise
        self._raw_observations.append({"path": path, "payload": payload})
        return payload

    def _read_ruleset(self, ruleset_id: int) -> dict[str, object]:
        path = f"{self._repository_path()}/rulesets/{ruleset_id}?includes_parents=true"
        try:
            return self._get_json(path)
        except ProviderDeliveryInspectionCapabilityError as error:
            if _caused_by_http_status(error, 404):
                raise _SemanticInconclusive("inherited_ruleset_visibility_unavailable") from error
            raise

    def _get_json(self, path: str) -> dict[str, object]:
        payload = self._get(path)
        if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
            raise _SemanticInconclusive("provider_response_malformed")
        return payload

    def _get_list(self, path: str) -> list[object]:
        payload = self._get(path)
        if not isinstance(payload, list):
            raise _SemanticInconclusive("provider_response_malformed")
        return payload

    def _require_repository_identity(self, payload: Mapping[str, object]) -> None:
        owner = _mapping(payload.get("owner"))
        if (
            _positive_int(payload.get("id")) != self._repository_id
            or _text(payload.get("full_name")).casefold() != self._repository.casefold()
            or _text(owner.get("login")).casefold() != self._owner.casefold()
            or _positive_int(owner.get("id")) != self._repository_owner_id
        ):
            raise _SemanticInconclusive("repository_identity_mismatch")
        for key in ("allow_merge_commit", "allow_squash_merge", "allow_rebase_merge"):
            if not isinstance(payload.get(key), bool):
                raise _SemanticInconclusive("provider_visibility_incomplete")

    def _require_branch_identity(self, payload: Mapping[str, object]) -> None:
        if _text(payload.get("name")) != self._base_branch or not isinstance(
            payload.get("protected"), bool
        ):
            raise _SemanticInconclusive("branch_identity_mismatch")

    def _evaluated_ruleset_ids(self, evaluated: list[object]) -> tuple[int, ...]:
        values: set[int] = set()
        for item in evaluated:
            entry = _mapping(item)
            ruleset_id = _positive_int(entry.get("ruleset_id"))
            if ruleset_id < 1:
                raise _SemanticInconclusive("provider_response_malformed")
            values.add(ruleset_id)
        return tuple(sorted(values))

    def _read_classic_protection(
        self, repository_payload: Mapping[str, object]
    ) -> _ClassicProtectionObservation | None:
        path = f"{self._repository_path()}/branches/{quote(self._base_branch, safe='')}/protection"
        try:
            payload = self._get_json(path)
        except ProviderDeliveryInspectionCapabilityError as error:
            if _caused_by_http_status(error, 404):
                self._raw_observations.append({"path": path, "status": 404})
                return None
            raise

        status_checks = self._get_optional_json(path + "/required_status_checks")
        pull_request_reviews = self._get_optional_json(path + "/required_pull_request_reviews")
        signatures = self._get_optional_json(path + "/required_signatures")

        # The aggregate protection response has no required field list. Reuse
        # its restriction aggregate when present, but prove an omitted value by
        # reading the dedicated endpoint rather than treating omission as none.
        raw_restrictions = payload.get("restrictions")
        restrictions: Mapping[str, object] | None
        if raw_restrictions is None:
            restrictions = self._get_optional_json(path + "/restrictions")
            if restrictions is not None:
                # A configured restriction omitted from the aggregate cannot
                # be followed by another paged read within the fixed 32-call
                # envelope at the 20-ruleset bound.
                raise _SemanticInconclusive("classic_response_incomplete")
        else:
            restrictions = _mapping(raw_restrictions)
        if restrictions is not None:
            owner = _mapping(repository_payload.get("owner"))
            if owner.get("type") != "Organization":
                raise _SemanticInconclusive("classic_response_incomplete")
            apps = self._get_list(path + f"/restrictions/apps?per_page={_RULE_PAGE_SIZE}")
            if len(apps) == _RULE_PAGE_SIZE:
                raise _SemanticInconclusive("classic_response_incomplete")
            _require_complete_classic_restrictions(aggregate=restrictions, apps=apps)
        return _ClassicProtectionObservation(
            overall=payload,
            status_checks=status_checks,
            pull_request_reviews=pull_request_reviews,
            signatures=signatures,
            restrictions=restrictions,
        )

    def _get_optional_json(self, path: str) -> dict[str, object] | None:
        try:
            return self._get_json(path)
        except ProviderDeliveryInspectionCapabilityError as error:
            if _caused_by_http_status(error, 404):
                self._raw_observations.append({"path": path, "status": 404})
                return None
            raise


class _NormalizedProtection:
    def __init__(
        self,
        *,
        update_ruleset_id: int,
        effective: ProviderDeliveryProtectionExpectationV1,
    ) -> None:
        self.update_ruleset_id = update_ruleset_id
        self.effective = effective


class _ClassicProtectionObservation:
    def __init__(
        self,
        *,
        overall: Mapping[str, object],
        status_checks: Mapping[str, object] | None,
        pull_request_reviews: Mapping[str, object] | None,
        signatures: Mapping[str, object] | None,
        restrictions: Mapping[str, object] | None,
    ) -> None:
        self.overall = overall
        self.status_checks = status_checks
        self.pull_request_reviews = pull_request_reviews
        self.signatures = signatures
        self.restrictions = restrictions


def _normalize_protection(
    *,
    rulesets: tuple[dict[str, object], ...],
    classic: _ClassicProtectionObservation | None,
    repository_payload: Mapping[str, object],
    repository: str,
    owner: str,
    ruleset_ids: tuple[int, ...],
    ordinary_delivery_app_id: int,
) -> _NormalizedProtection:
    status_checks: list[ProviderRequiredStatusCheckExpectationV1] = []
    scanning_tools: list[ProviderCodeScanningToolExpectationV1] = []
    pull_requests: list[ProviderPullRequestExpectationV1] = []
    pull_request_methods: list[set[str]] = []
    strict_checks = False
    deletion_protected = False
    non_fast_forward_protected = False
    update_ruleset_ids: list[int] = []

    for expected_id, ruleset in zip(ruleset_ids, rulesets, strict=True):
        if _positive_int(ruleset.get("id")) != expected_id:
            raise _SemanticInconclusive("provider_response_malformed")
        _require_ruleset_source(ruleset, repository=repository, owner=owner)
        if ruleset.get("enforcement") != "active" or ruleset.get("target") != "branch":
            raise _SemanticInconclusive("provider_visibility_incomplete")
        bypass_actors = _list(ruleset.get("bypass_actors"))
        raw_rules = _list(ruleset.get("rules"))
        rule_types = tuple(_text(_mapping(rule).get("type")) for rule in raw_rules)
        if not rule_types or any(rule_type not in _KNOWN_RULE_TYPES for rule_type in rule_types):
            raise _SemanticInconclusive("unknown_rule_type")
        if "update" in rule_types:
            update_ruleset_ids.append(expected_id)
            if rule_types != ("update",):
                raise _ProtectionNotReady("update_ruleset_not_isolated")
            _require_exclusive_update_bypass(
                bypass_actors,
                ordinary_delivery_app_id=ordinary_delivery_app_id,
            )
        elif bypass_actors:
            raise _ProtectionNotReady("gate_ruleset_bypass_present")
        for raw_rule in raw_rules:
            rule = _mapping(raw_rule)
            rule_type = _text(rule.get("type"))
            if rule_type == "deletion":
                deletion_protected = True
            elif rule_type == "non_fast_forward":
                non_fast_forward_protected = True
            elif rule_type == "required_status_checks":
                checks, strict = _ruleset_status_checks(rule)
                status_checks.extend(checks)
                strict_checks = strict_checks or strict
            elif rule_type == "code_scanning":
                scanning_tools.extend(_ruleset_scanning_tools(rule))
            elif rule_type == "pull_request":
                pull_request, rule_methods = _ruleset_pull_request(rule)
                pull_requests.append(pull_request)
                pull_request_methods.append(set(rule_methods))

    if len(update_ruleset_ids) != 1:
        reason: ProviderDeliveryInspectionReason = (
            "update_ruleset_missing" if not update_ruleset_ids else "multiple_update_rulesets"
        )
        raise _ProtectionNotReady(reason)

    if classic is not None:
        classic_values = _classic_protection(classic)
        status_checks.extend(classic_values[0])
        strict_checks = strict_checks or classic_values[1]
        if classic_values[2] is not None:
            pull_requests.append(classic_values[2])
        deletion_protected = deletion_protected or classic_values[3]
        non_fast_forward_protected = non_fast_forward_protected or classic_values[4]

    if not deletion_protected:
        raise _ProtectionNotReady("deletion_not_protected")
    if not non_fast_forward_protected:
        raise _ProtectionNotReady("non_fast_forward_not_protected")
    if len(pull_requests) > 1:
        raise _SemanticInconclusive("unsupported_protection_shape")
    status_checks = list(_deduplicate_status_checks(status_checks))
    if not status_checks:
        # All applicable rulesets and the dedicated classic endpoint were
        # observed completely, so this is known protection drift rather than a
        # schema ambiguity.
        raise _ProtectionNotReady("required_status_checks_mismatch")
    repository_methods = {
        method
        for method, field in (
            ("merge", "allow_merge_commit"),
            ("squash", "allow_squash_merge"),
            ("rebase", "allow_rebase_merge"),
        )
        if repository_payload.get(field) is True
    }
    allowed_methods = set(repository_methods)
    for method_set in pull_request_methods:
        allowed_methods &= method_set
    if "merge" not in allowed_methods:
        raise _ProtectionNotReady("allowed_merge_methods_mismatch")
    try:
        effective = ProviderDeliveryProtectionExpectationV1(
            required_status_checks=tuple(status_checks),
            strict_required_status_checks_policy=strict_checks,
            code_scanning_tools=tuple(scanning_tools),
            pull_request=pull_requests[0] if pull_requests else None,
            allowed_merge_methods=cast(
                tuple[MergeTrainMergeMethod, ...],
                tuple(
                    method for method in ("merge", "squash", "rebase") if method in allowed_methods
                ),
            ),
        )
    except ValidationError as error:
        raise _SemanticInconclusive("unsupported_protection_shape") from error
    return _NormalizedProtection(
        update_ruleset_id=update_ruleset_ids[0],
        effective=effective,
    )


def _readiness_reasons(
    actual: ProviderDeliveryProtectionExpectationV1,
    expected: ProviderDeliveryProtectionExpectationV1,
) -> tuple[ProviderDeliveryInspectionReason, ...]:
    reasons: list[ProviderDeliveryInspectionReason] = []
    if actual.required_status_checks != expected.required_status_checks:
        reasons.append("required_status_checks_mismatch")
    if actual.strict_required_status_checks_policy != expected.strict_required_status_checks_policy:
        reasons.append("strict_status_checks_mismatch")
    if actual.code_scanning_tools != expected.code_scanning_tools:
        reasons.append("code_scanning_mismatch")
    if actual.pull_request != expected.pull_request:
        reasons.append("pull_request_mismatch")
    if actual.allowed_merge_methods != expected.allowed_merge_methods:
        reasons.append("allowed_merge_methods_mismatch")
    return tuple(reasons)


def _ruleset_status_checks(
    rule: Mapping[str, object],
) -> tuple[tuple[ProviderRequiredStatusCheckExpectationV1, ...], bool]:
    parameters = _mapping(rule.get("parameters"))
    _require_only_keys(
        parameters,
        {
            "do_not_enforce_on_create",
            "required_status_checks",
            "strict_required_status_checks_policy",
        },
    )
    if parameters.get("do_not_enforce_on_create") is not False:
        raise _SemanticInconclusive("unsupported_protection_shape")
    strict = parameters.get("strict_required_status_checks_policy")
    if not isinstance(strict, bool):
        raise _SemanticInconclusive("provider_response_malformed")
    raw_checks = _list(parameters.get("required_status_checks"))
    if not raw_checks:
        raise _SemanticInconclusive("unsupported_protection_shape")
    checks: list[ProviderRequiredStatusCheckExpectationV1] = []
    for item in raw_checks:
        check = _mapping(item)
        _require_only_keys(check, {"context", "integration_id"})
        checks.append(
            ProviderRequiredStatusCheckExpectationV1(
                context=_text(check.get("context")),
                app_id=_positive_int(check.get("integration_id")),
            )
        )
    return tuple(checks), strict


def _ruleset_scanning_tools(
    rule: Mapping[str, object],
) -> tuple[ProviderCodeScanningToolExpectationV1, ...]:
    parameters = _mapping(rule.get("parameters"))
    _require_only_keys(parameters, {"code_scanning_tools"})
    raw_tools = _list(parameters.get("code_scanning_tools"))
    if not raw_tools:
        raise _SemanticInconclusive("unsupported_protection_shape")
    tools: list[ProviderCodeScanningToolExpectationV1] = []
    for item in raw_tools:
        tool = _mapping(item)
        _require_only_keys(tool, {"tool", "alerts_threshold", "security_alerts_threshold"})
        tools.append(
            ProviderCodeScanningToolExpectationV1.model_validate(
                {
                    "tool": _text(tool.get("tool")),
                    "alerts_threshold": _text(tool.get("alerts_threshold")),
                    "security_alerts_threshold": _text(tool.get("security_alerts_threshold")),
                }
            )
        )
    return tuple(tools)


def _ruleset_pull_request(
    rule: Mapping[str, object],
) -> tuple[ProviderPullRequestExpectationV1, tuple[str, ...]]:
    parameters = _mapping(rule.get("parameters"))
    _require_only_keys(
        parameters,
        {
            "allowed_merge_methods",
            "bypass_pull_request_allowances",
            "dismiss_stale_reviews_on_push",
            "dismissal_restrictions",
            "require_code_owner_review",
            "require_last_push_approval",
            "required_approving_review_count",
            "required_review_thread_resolution",
            "required_reviewers",
        },
    )
    for name in (
        "dismissal_restrictions",
        "bypass_pull_request_allowances",
        "required_reviewers",
    ):
        if name in parameters and parameters[name] not in (None, False, [], {}):
            raise _SemanticInconclusive("unsupported_protection_shape")
    methods = tuple(_text(item) for item in _list(parameters.get("allowed_merge_methods")))
    if (
        not methods
        or len(methods) != len(set(methods))
        or any(method not in {"merge", "squash", "rebase"} for method in methods)
    ):
        raise _SemanticInconclusive("unsupported_protection_shape")
    return (
        ProviderPullRequestExpectationV1(
            dismiss_stale_reviews_on_push=_bool(parameters.get("dismiss_stale_reviews_on_push")),
            require_code_owner_review=_bool(parameters.get("require_code_owner_review")),
            require_last_push_approval=_bool(parameters.get("require_last_push_approval")),
            required_approving_review_count=_nonnegative_int(
                parameters.get("required_approving_review_count")
            ),
            required_review_thread_resolution=_bool(
                parameters.get("required_review_thread_resolution")
            ),
        ),
        methods,
    )


def _classic_protection(
    observation: _ClassicProtectionObservation,
) -> tuple[
    tuple[ProviderRequiredStatusCheckExpectationV1, ...],
    bool,
    ProviderPullRequestExpectationV1 | None,
    bool,
    bool,
]:
    payload = observation.overall
    # These settings can independently block the merge-commit path. GitHub's
    # aggregate response does not require them, so an omission is unknown.
    relevant_flags = {
        "lock_branch",
        "required_conversation_resolution",
        "required_linear_history",
    }
    if relevant_flags - set(payload):
        raise _SemanticInconclusive("classic_response_incomplete")
    status_checks: tuple[ProviderRequiredStatusCheckExpectationV1, ...] = ()
    strict = False
    status = observation.status_checks
    review = observation.pull_request_reviews
    if status is not None:
        status_payload = _mapping(status)
        if {"strict", "contexts", "checks"} - set(status_payload):
            raise _SemanticInconclusive("classic_response_incomplete")
        strict = _bool(status_payload.get("strict"))
        contexts = tuple(_text(item) for item in _list(status_payload.get("contexts")))
        checks = tuple(
            ProviderRequiredStatusCheckExpectationV1(
                context=_text(_mapping(item).get("context")),
                app_id=_positive_int(_mapping(item).get("app_id")),
            )
            for item in _list(status_payload.get("checks"))
        )
        if {item.casefold() for item in contexts} != {item.context.casefold() for item in checks}:
            raise _SemanticInconclusive("classic_response_incomplete")
        status_checks = checks
    pull_request: ProviderPullRequestExpectationV1 | None = None
    if review is not None:
        review_payload = _mapping(review)
        required_review_fields = {
            "bypass_pull_request_allowances",
            "dismiss_stale_reviews",
            "dismissal_restrictions",
            "require_code_owner_reviews",
            "require_last_push_approval",
            "required_approving_review_count",
        }
        if required_review_fields - set(review_payload):
            raise _SemanticInconclusive("classic_response_incomplete")
        for name in ("dismissal_restrictions", "bypass_pull_request_allowances"):
            _require_empty_actor_collection(review_payload.get(name))
        conversation_enabled = _enabled_flag(payload["required_conversation_resolution"])
        pull_request = ProviderPullRequestExpectationV1(
            dismiss_stale_reviews_on_push=_bool(review_payload.get("dismiss_stale_reviews")),
            require_code_owner_review=_bool(review_payload.get("require_code_owner_reviews")),
            require_last_push_approval=_bool(review_payload.get("require_last_push_approval")),
            required_approving_review_count=_nonnegative_int(
                review_payload.get("required_approving_review_count")
            ),
            required_review_thread_resolution=conversation_enabled,
        )
    elif _enabled_flag(payload["required_conversation_resolution"]):
        raise _SemanticInconclusive("unsupported_protection_shape")
    if status is not None or review is not None:
        if "enforce_admins" not in payload:
            raise _SemanticInconclusive("classic_response_incomplete")
        if not _enabled_flag(payload["enforce_admins"]):
            raise _SemanticInconclusive("unsupported_protection_shape")
    if observation.signatures is not None and _enabled_flag(observation.signatures):
        raise _SemanticInconclusive("unsupported_protection_shape")
    if any(_enabled_flag(payload[name]) for name in ("required_linear_history", "lock_branch")):
        raise _SemanticInconclusive("unsupported_protection_shape")
    # These optional aggregate flags contribute a protection floor only when
    # they are actually visible. Rulesets must provide the floor otherwise.
    deletion_protected = "allow_deletions" in payload and not _enabled_flag(
        payload["allow_deletions"]
    )
    non_fast_forward_protected = "allow_force_pushes" in payload and not _enabled_flag(
        payload["allow_force_pushes"]
    )
    return (
        status_checks,
        strict,
        pull_request,
        deletion_protected,
        non_fast_forward_protected,
    )


def _require_ruleset_source(ruleset: Mapping[str, object], *, repository: str, owner: str) -> None:
    source_type = ruleset.get("source_type")
    source = _text(ruleset.get("source"))
    if source_type == "Repository" and source.casefold() == repository.casefold():
        return
    if source_type == "Organization" and source.casefold() == owner.casefold():
        return
    raise _SemanticInconclusive("unknown_ruleset_source")


def _require_exclusive_update_bypass(
    bypass_actors: list[object], *, ordinary_delivery_app_id: int
) -> None:
    if len(bypass_actors) != 1:
        raise _ProtectionNotReady("update_bypass_not_exclusive")
    actor = _mapping(bypass_actors[0])
    actor_type = actor.get("actor_type")
    if actor_type != "Integration":
        raise _SemanticInconclusive("unknown_bypass_actor")
    if (
        _positive_int(actor.get("actor_id")) != ordinary_delivery_app_id
        or actor.get("bypass_mode") != "pull_request"
    ):
        reason: ProviderDeliveryInspectionReason = (
            "unknown_bypass_mode"
            if actor.get("bypass_mode") != "pull_request"
            else "update_bypass_not_exclusive"
        )
        if reason == "unknown_bypass_mode":
            raise _SemanticInconclusive(reason)
        raise _ProtectionNotReady(reason)


def _require_complete_classic_restrictions(
    *, aggregate: Mapping[str, object], apps: list[object]
) -> None:
    if {"users", "teams", "apps"} - set(aggregate):
        raise _SemanticInconclusive("classic_response_incomplete")
    users = _list(aggregate.get("users"))
    teams = _list(aggregate.get("teams"))
    aggregate_apps = _list(aggregate.get("apps"))
    aggregate_ids = tuple(
        sorted(_positive_int(_mapping(item).get("id")) for item in aggregate_apps)
    )
    explicit_ids = tuple(sorted(_positive_int(_mapping(item).get("id")) for item in apps))
    if aggregate_ids != explicit_ids:
        raise _SemanticInconclusive("classic_response_incomplete")
    if users or teams or aggregate_ids:
        raise _SemanticInconclusive("unsupported_protection_shape")


def _deduplicate_status_checks(
    checks: list[ProviderRequiredStatusCheckExpectationV1],
) -> tuple[ProviderRequiredStatusCheckExpectationV1, ...]:
    unique: dict[tuple[str, int], ProviderRequiredStatusCheckExpectationV1] = {}
    for check in checks:
        key = (check.context.casefold(), check.app_id)
        previous = unique.get(key)
        if previous is not None and previous.context != check.context:
            raise _SemanticInconclusive("unsupported_protection_shape")
        unique[key] = check
    return tuple(unique.values())


def _require_empty_actor_collection(value: object) -> None:
    if value in (None, False, [], {}):
        return
    collection = _mapping(value)
    for key in ("users", "teams", "apps"):
        if key in collection and _list(collection[key]):
            raise _SemanticInconclusive("unsupported_protection_shape")


def _require_only_keys(value: Mapping[str, object], allowed: set[str]) -> None:
    if set(value) - allowed:
        raise _SemanticInconclusive("unsupported_protection_shape")


def _enabled_flag(value: object) -> bool:
    payload = _mapping(value)
    if not isinstance(payload.get("enabled"), bool):
        raise _SemanticInconclusive("classic_response_incomplete")
    return payload["enabled"] is True


def _normalize_target(
    *,
    repository: str,
    repository_id: int,
    repository_owner_id: int,
    base_branch: str,
    ordinary_delivery_app_id: int,
) -> tuple[str, str, str]:
    normalized_repository = repository.strip()
    normalized_branch = base_branch.strip()
    if (
        normalized_repository.count("/") != 1
        or isinstance(repository_id, bool)
        or not 0 < repository_id <= 2**63 - 1
        or isinstance(repository_owner_id, bool)
        or not 0 < repository_owner_id <= 2**63 - 1
        or isinstance(ordinary_delivery_app_id, bool)
        or not 0 < ordinary_delivery_app_id <= 2**63 - 1
        or not normalized_branch
        or len(normalized_branch) > 512
    ):
        raise ValueError("Provider-delivery inspection target is invalid.")
    owner, repo = normalized_repository.split("/", 1)
    if not owner or not repo:
        raise ValueError("Provider-delivery inspection repository is invalid.")
    return normalized_repository, owner, repo


def _remaining_timeout(remaining_seconds: RemainingSeconds) -> float:
    remaining = remaining_seconds()
    if (
        isinstance(remaining, bool)
        or not isinstance(remaining, (int, float))
        or not math.isfinite(remaining)
        or remaining <= 0
    ):
        raise ProviderDeliveryInspectionCapabilityError("provider_attempt_deadline")
    return min(float(remaining), _MAX_PER_REQUEST_SECONDS)


def _capability_error(
    error: Exception,
    *,
    utc_now: Callable[[], datetime],
) -> ProviderDeliveryInspectionCapabilityError:
    if isinstance(error, ProviderDeliveryInspectionCapabilityError):
        return error
    wait = provider_wait_observation_from_exception(error, utc_now=utc_now)
    if wait is not None or provider_error_is_quota_limited(error):
        return ProviderDeliveryInspectionCapabilityError(
            "provider_wait",
            retry_not_before=(
                _bounded_retry_not_before(wait.retry_not_before, utc_now=utc_now)
                if wait is not None
                else None
            ),
        )
    http_error = _chained_http_error(error)
    if http_error is not None and http_error.code in {401}:
        return ProviderDeliveryInspectionCapabilityError("provider_authentication_failed")
    if http_error is not None and http_error.code in {403}:
        return ProviderDeliveryInspectionCapabilityError("provider_permission_denied")
    if isinstance(error, (TimeoutError, socket.timeout)):
        return ProviderDeliveryInspectionCapabilityError("provider_attempt_deadline")
    return ProviderDeliveryInspectionCapabilityError("provider_transport")


def _bounded_retry_not_before(
    value: int,
    *,
    utc_now: Callable[[], datetime],
) -> int:
    now = math.ceil(utc_now().timestamp())
    return min(value, now + PROVIDER_INSPECTION_MAX_RETRY_AFTER_SECONDS)


def _error_retry_not_before(error: BaseException | None) -> int | None:
    return (
        error.retry_not_before
        if isinstance(error, ProviderDeliveryInspectionCapabilityError)
        else None
    )


def _strongest_retry_not_before(*values: int | None) -> int | None:
    return max((value for value in values if value is not None), default=None)


def _identity_capability_error(
    error: GitHubAppIdentityError,
) -> ProviderDeliveryInspectionCapabilityError:
    message = str(error).casefold()
    if "permission" in message:
        return ProviderDeliveryInspectionCapabilityError("provider_permission_denied")
    if (
        "private key" in message
        or "app id" in message
        or "another app" in message
        or "inventory owner" in message
    ):
        return ProviderDeliveryInspectionCapabilityError("provider_authentication_failed")
    return ProviderDeliveryInspectionCapabilityError("provider_transport")


def _provider_delivery_inspection_api_request(
    *,
    path: str,
    token: str,
    method: str = "GET",
    body: dict[str, object] | None = None,
    timeout_seconds: float,
) -> object:
    request_body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if body is not None:
        request_body = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        url=f"https://api.github.com{path}",
        method=method,
        headers=headers,
        data=request_body,
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        response_bytes = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(response_bytes) > _MAX_RESPONSE_BYTES:
            raise _ProviderResponseTooLarge("GitHub response exceeds inspection bound.")
        try:
            text = response_bytes.decode("utf-8")
            return json.loads(text) if text.strip() else None
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _ProviderResponseMalformed("GitHub response is not valid JSON.") from error


def _is_allowed_business_get(*, path: str, repository_path: str, base_branch: str) -> bool:
    encoded_branch = quote(base_branch, safe="")
    branch_path = f"{repository_path}/branches/{encoded_branch}"
    exact_paths = {
        repository_path,
        branch_path,
        f"{repository_path}/rules/branches/{encoded_branch}?per_page={_RULE_PAGE_SIZE}",
        branch_path + "/protection",
        branch_path + "/protection/required_status_checks",
        branch_path + "/protection/required_pull_request_reviews",
        branch_path + "/protection/required_signatures",
        branch_path + "/protection/restrictions",
        branch_path + f"/protection/restrictions/apps?per_page={_RULE_PAGE_SIZE}",
    }
    if path in exact_paths:
        return True
    prefix = repository_path + "/rulesets/"
    suffix = "?includes_parents=true"
    ruleset_id = path[len(prefix) : -len(suffix)] if path.startswith(prefix) else ""
    return path.endswith(suffix) and ruleset_id.isdecimal() and int(ruleset_id) > 0


def _chained_http_error(error: BaseException) -> HTTPError | None:
    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            return None
        seen.add(id(current))
        if isinstance(current, HTTPError):
            return current
        current = current.__cause__
    return None


def _caused_by_http_status(error: BaseException, status: int) -> bool:
    http_error = _chained_http_error(error)
    return http_error is not None and http_error.code == status


def _caused_by_type(error: BaseException, error_type: type[BaseException]) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            return False
        seen.add(id(current))
        if isinstance(current, error_type):
            return True
        current = current.__cause__
    return False


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _SemanticInconclusive("provider_response_malformed")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise _SemanticInconclusive("provider_response_malformed")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _SemanticInconclusive("provider_response_malformed")
    return value.strip()


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _SemanticInconclusive("provider_response_malformed")
    return value


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _SemanticInconclusive("provider_response_malformed")
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise _SemanticInconclusive("provider_response_malformed")
    return value
