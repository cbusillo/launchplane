"""Stateful provider and measurement support for ordinary-agent qualification."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Iterator, Mapping
from urllib.parse import unquote

from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.change_impact import (
    ChangeImpactComponentRule,
    ChangeImpactPolicyRecord,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.ordinary_agent import (
    OrdinaryAgentPolicyRule,
    OrdinaryAgentPullRequest,
    OrdinaryAgentTarget,
)
from control_plane.contracts.ordinary_agent_lifecycle import (
    OrdinaryAgentEnrollApplyEnvelope,
    OrdinaryAgentEnrollmentIntent,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentSessionAttenuation,
    OrdinaryAgentSessionDelegation,
)
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion
from control_plane.github_app_identity import ordinary_agent_effect_permissions
from control_plane.merge_train_github import MergeTrainGitHubTransport
from control_plane.ordinary_agent_github_transport import ordinary_provider_resource_class
from control_plane.ordinary_agent_authentication import (
    OrdinaryAgentTokenProof,
    parse_ordinary_agent_token,
)
from control_plane.ordinary_agent_session_approval import approve_ordinary_agent_enrollment
from control_plane.service_auth import (
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
    TerminalAgentIdentity,
)
from control_plane.service_human_auth import GitHubOAuthConfig, HumanSessionManager
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.ordinary_agent_lifecycle import (
    ADMIN_GITHUB_ID,
    enrollment_envelope,
    enrollment_mutation,
    prepare_approved_test_issuance,
)


def fixture_sha(label: str) -> str:
    """Return a deterministic commit-shaped identity for test fixture state."""
    return hashlib.sha1(label.encode(), usedforsecurity=False).hexdigest()


@dataclass
class QualificationClock:
    epoch: float = 1_789_000_000.0
    monotonic_seconds: float = 0.0
    request_seconds: float = 0.01

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.epoch, timezone.utc)

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def record_provider_call(self) -> None:
        self.epoch += self.request_seconds
        self.monotonic_seconds += self.request_seconds

    def advance_to(self, epoch: int) -> None:
        if epoch > self.epoch:
            elapsed = epoch - self.epoch
            self.epoch = float(epoch)
            self.monotonic_seconds += elapsed


@dataclass(frozen=True)
class QualificationContext:
    worker_id: str
    request_id: str
    repository: str


@dataclass
class ProviderCall:
    sequence: int
    worker_id: str
    request_id: str
    repository: str
    authority_kind: str
    authority_id: int
    method: str
    path: str
    phase: str
    resource_class: str
    started_at: float
    body: dict[str, object] | None = field(repr=False)
    graphql_cost: int = 0
    completed_at: float | None = None
    outcome: str = "pending"
    error_type: str | None = None


@dataclass
class ProviderFailure:
    repository: str
    phase: str
    error: Exception
    after_dispatch: bool = False


@dataclass
class PullRequestState:
    number: int
    head_sha: str
    head_tree_sha: str
    author_id: int
    author_login: str
    bound: bool
    state: str = "OPEN"
    merge_commit_sha: str | None = None
    base_sha_at_merge: str | None = None


@dataclass
class RepositoryScenario:
    repository_id: int
    owner_id: int
    repository: str
    installation_id: int
    base_branch: str
    base_sha: str
    base_tree_sha: str
    pull_requests: dict[int, PullRequestState]
    refs: dict[str, str] = field(default_factory=dict)
    commits: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)
    candidate_no_op_pull_requests: set[int] = field(default_factory=set)

    @classmethod
    def build(
        cls,
        *,
        repository_id: int,
        owner_id: int,
        repository: str,
        installation_id: int,
        first_pull_request: int,
        admin_id: int,
    ) -> RepositoryScenario:
        base_sha = fixture_sha(f"{repository}:base")
        base_tree = fixture_sha(f"{repository}:base-tree")
        pull_requests = {
            number: PullRequestState(
                number=number,
                head_sha=fixture_sha(f"{repository}:pr:{number}:head"),
                head_tree_sha=fixture_sha(f"{repository}:pr:{number}:tree"),
                author_id=admin_id,
                author_login=f"qualification-admin-{admin_id}",
                bound=index < 2,
            )
            for index, number in enumerate(range(first_pull_request, first_pull_request + 4))
        }
        commits: dict[str, tuple[str, tuple[str, ...]]] = {base_sha: (base_tree, ())}
        commits.update(
            {
                pull_request.head_sha: (pull_request.head_tree_sha, (base_sha,))
                for pull_request in pull_requests.values()
            }
        )
        return cls(
            repository_id=repository_id,
            owner_id=owner_id,
            repository=repository,
            installation_id=installation_id,
            base_branch="main",
            base_sha=base_sha,
            base_tree_sha=base_tree,
            pull_requests=pull_requests,
            commits=commits,
        )

    @property
    def bound_pull_requests(self) -> tuple[PullRequestState, ...]:
        return tuple(item for item in self.pull_requests.values() if item.bound)

    @property
    def unbound_pull_requests(self) -> tuple[PullRequestState, ...]:
        return tuple(item for item in self.pull_requests.values() if not item.bound)

    def configure_candidate_no_op(self, pull_request_number: int) -> None:
        pull_request = self.pull_requests[pull_request_number]
        if not pull_request.bound:
            raise AssertionError("qualification no-op must address a bound pull request")
        self.commits[pull_request.head_sha] = (pull_request.head_tree_sha, ())
        self.commits[self.base_sha] = (self.base_tree_sha, (pull_request.head_sha,))
        self.candidate_no_op_pull_requests.add(pull_request_number)


@dataclass(frozen=True)
class OrdinaryQualificationFleet:
    store: PostgresRecordStore
    clock: QualificationClock
    provider: MeasuredOrdinaryGitHubScenario
    requests: tuple[OrdinaryAgentFiniteRequestRecord, ...]
    proofs: tuple[OrdinaryAgentTokenProof, ...] = field(repr=False)
    merge_policy: MergeTrainPolicyRecord

    @classmethod
    def create(
        cls,
        *,
        store: PostgresRecordStore,
        clock: QualificationClock,
        repository_count: int = 6,
        request_expires_in: int = 10_000,
        continuation_expires_in: int = 20_000,
    ) -> OrdinaryQualificationFleet:
        if not 1 <= repository_count <= 6:
            raise ValueError("qualification repository count must be between one and six")
        repositories = tuple(
            RepositoryScenario.build(
                repository_id=91_100 + index,
                owner_id=92_100 + index // 2,
                repository=f"qualification-owner-{index // 2 + 1}/repository-{index + 1}",
                installation_id=93_100 + index,
                first_pull_request=100 + index * 10,
                admin_id=94_100 + index // 2,
            )
            for index in range(repository_count)
        )
        authz_policy = _qualification_authz_policy(repositories)
        # Schema-v3 policy writes are deliberately not activated in production.
        # Existing lifecycle tests seed the exact typed row through this test-only
        # helper while all later enrollment/request writes use public operations.
        store._write_row(store._authz_policy_row(authz_policy))
        inventories = tuple(_write_inventory(store, repository) for repository in repositories)
        _write_change_impact_policies(store, repositories)
        _write_managed_secret(store)
        merge_policy = _write_merge_policy(store, repositories)
        requests, proofs = _enroll_and_admit(
            store=store,
            clock=clock,
            authz_policy=authz_policy,
            repositories=repositories,
            inventories=inventories,
            request_expires_in=request_expires_in,
            continuation_expires_in=continuation_expires_in,
        )
        return cls(
            store=store,
            clock=clock,
            provider=MeasuredOrdinaryGitHubScenario(
                repositories=repositories,
                clock=clock,
            ),
            requests=requests,
            proofs=proofs,
            merge_policy=merge_policy,
        )


def _qualification_authz_policy(
    repositories: tuple[RepositoryScenario, ...],
) -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy(
        schema_version=3,
        github_humans=(
            GitHubHumanPolicyRule(
                managed_set_id="ordinary-agent.administrators",
                managed_rule_id="owner",
                github_ids=(ADMIN_GITHUB_ID,),
                roles=("admin",),
                products=("launchplane",),
                contexts=("launchplane",),
                actions=("authz_policy_grant.write",),
            ),
        ),
        ordinary_agents=tuple(
            OrdinaryAgentPolicyRule(
                managed_set_id="ordinary-agent.qualification",
                managed_rule_id=f"agent_{index}.qualification.main",
                principal_id=f"agent_{index}",
                target=_target(repository),
                actions=("self_read", "preflight", "guarded_merge"),
            )
            for index, repository in enumerate(repositories, start=1)
        ),
    )
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
        revision=1,
        source="test:ordinary-agent-six-repository-qualification",
        updated_at="2026-09-09T00:00:00Z",
        policy_sha256=digest,
        policy=policy,
    )


def _target(repository: RepositoryScenario) -> OrdinaryAgentTarget:
    return OrdinaryAgentTarget(
        repository_id=repository.repository_id,
        repository=repository.repository,
        base_branch=repository.base_branch,
    )


def _write_inventory(
    store: PostgresRecordStore, repository: RepositoryScenario
) -> RepositoryInventoryRecord:
    inventory = RepositoryInventoryRecord.model_validate(
        {
            "repository_id": str(repository.repository_id),
            "repository_owner_id": str(repository.owner_id),
            "repository": repository.repository,
            "inventory_state": "tracked",
            "inventory_revision": 1,
            "recorded_at": "2026-09-09T00:00:00Z",
            "source": "test:ordinary-agent-six-repository-qualification",
            "reason": "exercise exact six-repository ordinary authority",
        }
    )
    store.write_repository_inventory_record(inventory)
    return inventory


def _write_managed_secret(store: PostgresRecordStore) -> None:
    store.write_secret_version(
        SecretVersion(
            version_id="ordinary-agent-app-key-v1",
            secret_id="ordinary-agent-app-key",
            created_at="2026-09-09T00:00:00Z",
            created_by="test",
            ciphertext="encrypted-test-placeholder",
        )
    )
    store.write_secret_record(
        SecretRecord(
            secret_id="ordinary-agent-app-key",
            scope="global",
            integration="ordinary_agent_github_app",
            name="ordinary-agent-app-key",
            current_version_id="ordinary-agent-app-key-v1",
            created_at="2026-09-09T00:00:00Z",
            updated_at="2026-09-09T00:00:00Z",
            updated_by="test",
        )
    )
    store.write_secret_binding(
        SecretBinding(
            binding_id="ordinary-agent-app-key-binding",
            secret_id="ordinary-agent-app-key",
            integration="ordinary_agent_github_app",
            binding_key="private_key",
            status="configured",
            created_at="2026-09-09T00:00:00Z",
            updated_at="2026-09-09T00:00:00Z",
        )
    )


def _write_change_impact_policies(
    store: PostgresRecordStore, repositories: tuple[RepositoryScenario, ...]
) -> None:
    for repository in repositories:
        store.write_change_impact_policy_record(
            ChangeImpactPolicyRecord(
                repository_id=str(repository.repository_id),
                repository_owner_id=str(repository.owner_id),
                repository=repository.repository,
                policy_revision=1,
                component_rules=(
                    ChangeImpactComponentRule(
                        component="qualification",
                        path_prefixes=("src",),
                        affected_products=(),
                        review_tier="routine",
                        reason="Qualification changes require engineering evidence only.",
                    ),
                ),
                effective_at="2026-09-09T00:00:00Z",
                source="test:ordinary-agent-six-repository-qualification",
                reason="Exercise real change-impact policy evaluation without product impact.",
            )
        )


def _write_merge_policy(
    store: PostgresRecordStore, repositories: tuple[RepositoryScenario, ...]
) -> MergeTrainPolicyRecord:
    record = MergeTrainPolicyRecord.model_validate(
        {
            "record_id": "ordinary-agent-six-repository-policy",
            "source": "test:ordinary-agent-six-repository-qualification",
            "updated_at": "2026-09-09T00:00:00Z",
            "policy": {
                "policies": [
                    {
                        "repository": repository.repository,
                        "base_branch": repository.base_branch,
                        "enqueue_label": "queue",
                        "blocked_label": "blocked",
                        "merge_method": "merge",
                        "failure_policy": "pause_train",
                        "enqueue": {},
                        "merge_identity": {"kind": "github_app", "name": "qualification"},
                    }
                    for repository in repositories
                ]
            },
        }
    )
    store.write_merge_train_policy_record(record)
    return record


def _enroll_and_admit(
    *,
    store: PostgresRecordStore,
    clock: QualificationClock,
    authz_policy: LaunchplaneAuthzPolicyRecord,
    repositories: tuple[RepositoryScenario, ...],
    inventories: tuple[RepositoryInventoryRecord, ...],
    request_expires_in: int,
    continuation_expires_in: int,
) -> tuple[tuple[OrdinaryAgentFiniteRequestRecord, ...], tuple[OrdinaryAgentTokenProof, ...]]:
    manager = HumanSessionManager(
        config=GitHubOAuthConfig(
            client_id="test",
            client_secret="test",
            public_url="https://example.test",
            session_secret="qualification-session-secret",
        ),
        session_store=store,
        now=clock.now,
    )
    human = manager.issue(
        GitHubHumanIdentity(
            login="qualification-admin",
            github_id=ADMIN_GITHUB_ID,
            name="Qualification Admin",
            email="qualification@example.test",
            organizations=frozenset(),
            teams=frozenset(),
            role="admin",
        )
    )
    now = int(clock.epoch)
    requests: list[OrdinaryAgentFiniteRequestRecord] = []
    proofs: list[OrdinaryAgentTokenProof] = []
    for index, (repository, inventory) in enumerate(
        zip(repositories, inventories, strict=True), start=1
    ):
        principal_id = f"agent_{index}"
        operation_id = f"ordinary-agent-qualification-enroll-{index}"
        envelope = enrollment_envelope(
            policy_record=authz_policy,
            inventory=inventory,
            operation_id=operation_id,
            credential_digest=hashlib.sha256(principal_id.encode()).hexdigest(),
            principal_id=principal_id,
            managed_set_id="ordinary-agent.qualification",
            managed_rule_id=f"{principal_id}.qualification.main",
            target=_target(repository),
            github_app_id=MeasuredOrdinaryGitHubScenario.app_id,
            github_installation_id=repository.installation_id,
            delivery_expires_at=now + 600,
        )
        delegation = OrdinaryAgentSessionDelegation(
            operation_id=operation_id,
            approval_sha256=envelope.approval_sha256,
            receiver_sha256=envelope.delivery.receiver_claim_sha256,
            actions=("guarded_merge",),
            session_expires_at=now + 10_000,
            lease_expires_at=now + 10_000,
            action_limit=6,
            pull_request_limit=2,
            refresh_allowance=1,
            continuation_expires_at=now + 20_000,
        )
        attenuation = OrdinaryAgentSessionAttenuation.model_validate(
            delegation.model_dump(exclude={"operation_id", "approval_sha256", "receiver_sha256"})
        )
        intent = OrdinaryAgentEnrollmentIntent.from_envelope(
            envelope.model_copy(update={"session_attenuation": attenuation})
        )
        store.propose_ordinary_agent_enrollment(
            intent=intent,
            requester=TerminalAgentIdentity(subject="qualification", token_label="test"),
        )
        approved = approve_ordinary_agent_enrollment(
            store=store,
            manager=manager,
            cookie_header=manager.session_cookie_header(human),
            csrf_token=manager.csrf_token(human),
            principal_id=principal_id,
            operation_id=operation_id,
        )
        approved_envelope, issuance = prepare_approved_test_issuance(approved)
        if not isinstance(approved_envelope, OrdinaryAgentEnrollApplyEnvelope):
            raise AssertionError(
                f"qualification enrollment {index} resolved as {type(approved_envelope).__name__}"
            )
        applied = store.compare_and_apply_ordinary_agent_enrollment(
            envelope=approved_envelope,
            mutation=enrollment_mutation(approved_envelope),
            issuance=issuance,
        )
        if applied.status != "written":
            raise AssertionError(f"qualification enrollment {index} was not written: {applied!r}")
        proof = parse_ordinary_agent_token(issuance.token.value)
        issued = store.reconnect_ordinary_agent_session(proof=proof, operation_id=operation_id)
        lease = issued.leases[0]
        request = OrdinaryAgentFiniteRequestRecord(
            request_id=f"qualification-request-{index}",
            idempotency_key=f"qualification-request-{index}",
            principal_id=principal_id,
            session_id=issued.session.session_id,
            lease_id=lease.lease_id,
            target=lease.target,
            base_sha=repository.base_sha,
            pull_requests=tuple(
                OrdinaryAgentPullRequest(number=item.number, head_sha=item.head_sha)
                for item in repository.bound_pull_requests
            ),
            permitted_stack_edit_pull_requests=(),
            refresh_allowance_total=1,
            admitted_at=now,
            expires_at=now + request_expires_in,
            continuation_expires_at=now + continuation_expires_in,
        )
        requests.append(store.admit_ordinary_agent_finite_request(proof=proof, request=request))
        proofs.append(proof)
    return tuple(requests), tuple(proofs)


class _QualificationTransport:
    def __init__(self, scenario: MeasuredOrdinaryGitHubScenario, token: str) -> None:
        self._scenario = scenario
        self._token = token

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        return self._scenario.request(token=self._token, method=method, path=path, body=body)


class MeasuredOrdinaryGitHubScenario:
    """A semantic six-repository GitHub fake with one complete call ledger."""

    app_id = 123_456
    installation_permissions = {
        "administration": "read",
        "checks": "read",
        "contents": "write",
        "metadata": "read",
        "pull_requests": "write",
        "statuses": "read",
    }

    def __init__(
        self, *, repositories: tuple[RepositoryScenario, ...], clock: QualificationClock
    ) -> None:
        self.repositories = {item.repository: item for item in repositories}
        self._by_installation = {item.installation_id: item for item in repositories}
        self._tokens: dict[str, RepositoryScenario] = {}
        self._revoked_tokens: set[str] = set()
        self._token_sequence = 0
        self._context: ContextVar[QualificationContext | None] = ContextVar(
            "ordinary_qualification_context", default=None
        )
        self.clock = clock
        self.calls: list[ProviderCall] = []
        self._failures: list[ProviderFailure] = []

    def fail_next(
        self,
        *,
        repository: str,
        phase: str,
        error: Exception,
        after_dispatch: bool = False,
    ) -> None:
        self._failures.append(
            ProviderFailure(
                repository=repository,
                phase=phase,
                error=error,
                after_dispatch=after_dispatch,
            )
        )

    @contextmanager
    def bind(self, *, worker_id: str, request_id: str, repository: str) -> Iterator[None]:
        token = self._context.set(
            QualificationContext(worker_id=worker_id, request_id=request_id, repository=repository)
        )
        try:
            yield
        finally:
            self._context.reset(token)

    def transport_for(self, token: str) -> MergeTrainGitHubTransport:
        if token not in self._tokens:
            raise AssertionError("qualification transport received an unknown token")
        return _QualificationTransport(self, token)

    def api_request(self, **kwargs: object) -> object:
        path = str(kwargs["path"])
        method = str(kwargs.get("method", "GET"))
        body = kwargs.get("body")
        typed_body = body if isinstance(body, dict) else None
        context = self._require_context()
        scenario = self.repositories[context.repository]
        raw_token = kwargs.get("token")
        if (
            isinstance(raw_token, str)
            and raw_token in self._tokens
            and path != "/installation/token"
        ):
            return self.request(
                token=raw_token,
                method=method,
                path=path,
                body=typed_body,
            )
        result: object
        if path == f"/repos/{scenario.repository}/installation" and method == "GET":
            phase = "installation_lookup"
            authority_kind, authority_id = "app", self.app_id
        elif (
            path == f"/app/installations/{scenario.installation_id}/access_tokens"
            and method == "POST"
        ):
            phase = "token_mint"
            authority_kind, authority_id = "app", self.app_id
        elif path == "/installation/token" and method == "DELETE":
            phase = "token_revoke"
            authority_kind, authority_id = "installation", scenario.installation_id
        else:
            raise AssertionError(f"unexpected qualification API request: {method} {path}")
        call = self._begin_call(
            context=context,
            scenario=scenario,
            method=method,
            path=path,
            body=typed_body,
            phase=phase,
            authority_kind=authority_kind,
            authority_id=authority_id,
        )
        try:
            self._raise_injected_failure(call, after_dispatch=False)
            if phase == "installation_lookup":
                result = {
                    "id": scenario.installation_id,
                    "app_id": self.app_id,
                    "permissions": dict(self.installation_permissions),
                }
            elif phase == "token_mint":
                result = self._mint_token(scenario=scenario, body=typed_body)
            else:
                raw_token = kwargs.get("token")
                if not isinstance(raw_token, str) or self._tokens.get(raw_token) is not scenario:
                    raise AssertionError("qualification revoke used the wrong repository token")
                self._tokens.pop(raw_token)
                self._revoked_tokens.add(raw_token)
                result = None
        except Exception as error:
            self._finish_call(call, error=error)
            raise
        self._finish_call(call)
        return result

    def _mint_token(
        self, *, scenario: RepositoryScenario, body: dict[str, object] | None
    ) -> dict[str, object]:
        typed_body = body
        if typed_body is None or typed_body.get("repository_ids") != [scenario.repository_id]:
            raise AssertionError("qualification token mint escaped its repository")
        requested = typed_body.get("permissions")
        if not isinstance(requested, dict):
            raise AssertionError("qualification token mint omitted permissions")
        self._token_sequence += 1
        token = f"qualification-token-{scenario.installation_id}-{self._token_sequence}"
        self._tokens[token] = scenario
        return {
            "token": token,
            "expires_at": (self.clock.now() + timedelta(minutes=5)).isoformat(),
            "permissions": {"metadata": "read", **requested},
            "repositories": [{"id": scenario.repository_id, "full_name": scenario.repository}],
        }

    def request(
        self,
        *,
        token: str,
        method: str,
        path: str,
        body: dict[str, object] | None,
    ) -> object:
        context = self._require_context()
        scenario = self._tokens.get(token)
        if (
            token in self._revoked_tokens
            or scenario is None
            or scenario.repository != context.repository
        ):
            raise AssertionError("qualification provider token escaped its repository")
        phase = self._request_phase(scenario=scenario, method=method, path=path, body=body)
        call = self._begin_call(
            context=context,
            scenario=scenario,
            method=method,
            path=path,
            body=body,
            phase=phase,
            authority_kind="installation",
            authority_id=scenario.installation_id,
        )
        try:
            self._raise_injected_failure(call, after_dispatch=False)
            result, dispatched_phase, graphql_cost = self._dispatch(
                scenario=scenario, method=method, path=path, body=body
            )
            if dispatched_phase != phase:
                raise AssertionError("qualification request phase classification drifted")
            self._raise_injected_failure(call, after_dispatch=True)
        except Exception as error:
            self._finish_call(call, error=error)
            raise
        self._finish_call(call, graphql_cost=graphql_cost)
        return result

    def _request_phase(
        self,
        *,
        scenario: RepositoryScenario,
        method: str,
        path: str,
        body: dict[str, object] | None,
    ) -> str:
        if path == "/graphql" and method == "POST":
            if body is None or not isinstance(body.get("query"), str):
                return "graphql_malformed"
            query = str(body["query"])
            variables = body.get("variables")
            if not isinstance(variables, dict):
                return "graphql_malformed"
            if "parents(first: 3)" in query:
                return "landing_proof"
            if "candidate" in variables and "number0" not in variables:
                return "candidate_check"
            if "candidate" in variables:
                return "landing_read" if "candidateRef" in variables else "landing_confirm"
            return "snapshot"
        prefix = f"/repos/{scenario.repository}"
        if path == f"{prefix}/rules/branches/{scenario.base_branch}?per_page=100":
            return "rules_read"
        if path == f"{prefix}/collaborators?permission=admin&per_page=100&page=1":
            return "admins_read"
        if path == f"{prefix}/git/refs" and method == "POST":
            return "candidate_prepare"
        if path == f"{prefix}/merges" and method == "POST":
            return "candidate_merge"
        if path.startswith(f"{prefix}/pulls/") and path.endswith("/files?per_page=100&page=1"):
            return "landing_files"
        if path.startswith(f"{prefix}/pulls/") and path.endswith("/commits?per_page=100&page=1"):
            return "landing_commits"
        if path.startswith(f"{prefix}/pulls/") and path.endswith("/merge") and method == "PUT":
            return "landing_merge"
        if path.startswith(f"{prefix}/pulls/") and method == "GET":
            return "reconciliation_pull_request"
        if path.startswith(f"{prefix}/git/ref/") and method == "GET":
            return "reconciliation_ref"
        if path.startswith(f"{prefix}/git/commits/") and method == "GET":
            return "reconciliation_commit"
        if path.startswith(f"{prefix}/compare/") and method == "GET":
            return "reconciliation_compare"
        return "unexpected"

    def _dispatch(
        self,
        *,
        scenario: RepositoryScenario,
        method: str,
        path: str,
        body: dict[str, object] | None,
    ) -> tuple[object, str, int]:
        if path == "/graphql" and method == "POST":
            return self._graphql(scenario=scenario, body=body)
        prefix = f"/repos/{scenario.repository}"
        if path == f"{prefix}/rules/branches/{scenario.base_branch}?per_page=100":
            return [], "rules_read", 0
        if path == f"{prefix}/collaborators?permission=admin&per_page=100&page=1":
            admins = {
                (item.author_id, item.author_login) for item in scenario.pull_requests.values()
            }
            return (
                [
                    {"id": admin_id, "login": login, "permissions": {"admin": True}}
                    for admin_id, login in sorted(admins)
                ],
                "admins_read",
                0,
            )
        if path == f"{prefix}/git/refs" and method == "POST":
            if body is None or not isinstance(body.get("ref"), str):
                raise AssertionError("qualification candidate ref body is malformed")
            reference = str(body["ref"])
            sha = str(body.get("sha", ""))
            if sha != scenario.base_sha:
                raise AssertionError("qualification candidate ref used a stale base")
            scenario.refs[reference] = sha
            return {"ref": reference, "object": {"sha": sha}}, "candidate_prepare", 0
        if path == f"{prefix}/merges" and method == "POST":
            return self._merge_candidate(scenario=scenario, body=body), "candidate_merge", 0
        if path.startswith(f"{prefix}/pulls/") and path.endswith("/files?per_page=100&page=1"):
            number = self._path_pull_request_number(path)
            self._require_bound(scenario, number)
            return (
                [{"filename": f"src/pr-{number}.py", "status": "modified"}],
                "landing_files",
                0,
            )
        if path.startswith(f"{prefix}/pulls/") and path.endswith("/commits?per_page=100&page=1"):
            number = self._path_pull_request_number(path)
            pull_request = self._require_bound(scenario, number)
            user = {
                "id": pull_request.author_id,
                "login": pull_request.author_login,
                "type": "User",
            }
            return (
                [{"sha": pull_request.head_sha, "author": user, "committer": user}],
                "landing_commits",
                0,
            )
        if path.startswith(f"{prefix}/pulls/") and path.endswith("/merge") and method == "PUT":
            number = self._path_pull_request_number(path)
            return self._land(scenario=scenario, number=number, body=body), "landing_merge", 0
        if path.startswith(f"{prefix}/pulls/") and method == "GET":
            number = self._path_pull_request_number(path)
            pull_request = self._require_bound(scenario, number)
            return (
                self._reconciliation_pull_request(scenario, pull_request),
                ("reconciliation_pull_request"),
                0,
            )
        if path.startswith(f"{prefix}/git/ref/") and method == "GET":
            reference = "refs/" + unquote(path.split("/git/ref/", 1)[1])
            observed_sha = (
                scenario.base_sha
                if reference == f"refs/heads/{scenario.base_branch}"
                else scenario.refs.get(reference)
            )
            if observed_sha is None:
                raise AssertionError("qualification reconciliation used an unknown ref")
            return (
                {
                    "ref": reference,
                    "object": {"sha": observed_sha},
                },
                "reconciliation_ref",
                0,
            )
        if path.startswith(f"{prefix}/git/commits/") and method == "GET":
            sha = unquote(path.split("/git/commits/", 1)[1])
            tree, parents = self._require_commit(scenario, sha)
            return (
                {
                    "sha": sha,
                    "tree": {"sha": tree},
                    "parents": [{"sha": parent} for parent in parents],
                    "message": "qualification commit",
                },
                "reconciliation_commit",
                0,
            )
        if path.startswith(f"{prefix}/compare/") and method == "GET":
            comparison = unquote(path.split("/compare/", 1)[1])
            base_sha, head_sha = comparison.split("...", 1)
            status = "ahead" if self._contains(scenario, head_sha, base_sha) else "diverged"
            merge_base = base_sha if status == "ahead" else scenario.base_sha
            return (
                {
                    "status": status,
                    "base_commit": {"sha": base_sha},
                    "merge_base_commit": {"sha": merge_base},
                },
                "reconciliation_compare",
                0,
            )
        raise AssertionError(f"unexpected qualification transport request: {method} {path}")

    def _graphql(
        self, *, scenario: RepositoryScenario, body: dict[str, object] | None
    ) -> tuple[object, str, int]:
        if body is None or not isinstance(body.get("query"), str):
            raise AssertionError("qualification GraphQL body is malformed")
        query = str(body["query"])
        variables = body.get("variables")
        if not isinstance(variables, dict):
            raise AssertionError("qualification GraphQL variables are malformed")
        self._require_graphql_scope(scenario, variables)
        cost = 1
        if "parents(first: 3)" in query:
            repository = {
                "databaseId": scenario.repository_id,
                "ref": {"target": self._commit_payload(scenario, scenario.base_sha, parents=True)},
            }
            phase = "landing_proof"
        elif "candidate" in variables and "number0" not in variables:
            repository = self._candidate_check_payload(scenario, variables)
            phase = "candidate_check"
        elif "candidate" in variables:
            repository = self._landing_payload(
                scenario, variables, detailed="candidateRef" in variables
            )
            phase = "landing_read" if "candidateRef" in variables else "landing_confirm"
        else:
            repository = self._snapshot_payload(scenario, variables)
            phase = "snapshot"
        return {"data": {"rateLimit": {"cost": cost}, "repository": repository}}, phase, cost

    def _snapshot_payload(
        self, scenario: RepositoryScenario, variables: Mapping[str, object]
    ) -> dict[str, object]:
        repository = self._repository_identity(scenario)
        comparisons: dict[str, object] = {}
        for index, pull_request in enumerate(self._requested_pull_requests(scenario, variables)):
            repository[f"pr{index}"] = {
                **self._pull_request_payload(scenario, pull_request, detailed=True),
                "mergeable": "MERGEABLE",
            }
            repository[f"head{index}"] = self._commit_payload(
                scenario, pull_request.head_sha, checks=True
            )
            comparisons[f"compare{index}"] = self._comparison(
                scenario.base_sha, pull_request.head_sha, "AHEAD"
            )
        repository["ref"] = {
            "name": scenario.base_branch,
            "target": self._commit_payload(scenario, scenario.base_sha),
            "branchProtectionRule": self._protection(),
            **comparisons,
        }
        return repository

    def _candidate_check_payload(
        self, scenario: RepositoryScenario, variables: Mapping[str, object]
    ) -> dict[str, object]:
        candidate_sha = str(variables["candidate"])
        self._require_commit(scenario, candidate_sha)
        repository = self._repository_identity(scenario)
        repository["ref"] = {
            "name": scenario.base_branch,
            "target": self._commit_payload(scenario, scenario.base_sha),
            "branchProtectionRule": self._protection(),
            "compare": self._comparison(scenario.base_sha, candidate_sha, "AHEAD"),
        }
        repository["candidate"] = self._commit_payload(scenario, candidate_sha, checks=True)
        return repository

    def _landing_payload(
        self,
        scenario: RepositoryScenario,
        variables: Mapping[str, object],
        *,
        detailed: bool,
    ) -> dict[str, object]:
        candidate_sha = str(variables["candidate"])
        self._require_commit(scenario, candidate_sha)
        repository = self._repository_identity(scenario)
        repository["candidate"] = self._commit_payload(scenario, candidate_sha, checks=detailed)
        ref: dict[str, object] = {
            "name": scenario.base_branch,
            "target": self._commit_payload(scenario, scenario.base_sha),
        }
        if detailed:
            comparison_status = (
                "DIVERGED"
                if any(item.state == "MERGED" for item in scenario.bound_pull_requests)
                else "AHEAD"
            )
            ref.update(
                branchProtectionRule=self._protection(),
                compare=self._comparison(scenario.base_sha, candidate_sha, comparison_status),
            )
        repository["ref"] = ref
        for index, pull_request in enumerate(self._requested_pull_requests(scenario, variables)):
            repository[f"pr{index}"] = self._pull_request_payload(
                scenario, pull_request, detailed=detailed
            )
            repository[f"head{index}"] = self._commit_payload(scenario, pull_request.head_sha)
        return repository

    def _merge_candidate(
        self, *, scenario: RepositoryScenario, body: dict[str, object] | None
    ) -> dict[str, object] | None:
        if body is None:
            raise AssertionError("qualification candidate merge omitted a body")
        head_sha = str(body.get("head", ""))
        pull_request = next(
            (item for item in scenario.bound_pull_requests if item.head_sha == head_sha), None
        )
        if pull_request is None:
            raise AssertionError("qualification candidate merge used an unbound head")
        branch = str(body.get("base", ""))
        reference = f"refs/heads/{unquote(branch)}"
        parent_sha = scenario.refs.get(reference)
        if parent_sha is None:
            raise AssertionError("qualification candidate merge used an unknown ref")
        if pull_request.number in scenario.candidate_no_op_pull_requests:
            if not self._contains(scenario, parent_sha, pull_request.head_sha):
                raise AssertionError("qualification no-op head is not contained by candidate")
            return None
        result_sha = fixture_sha(f"{scenario.repository}:candidate:{pull_request.number}")
        result_tree = fixture_sha(f"{scenario.repository}:candidate-tree:{pull_request.number}")
        scenario.refs[reference] = result_sha
        scenario.commits[result_sha] = (result_tree, (parent_sha, head_sha))
        return {
            "sha": result_sha,
            "tree": {"sha": result_tree},
            "parents": [{"sha": parent_sha}, {"sha": head_sha}],
        }

    def _land(
        self,
        *,
        scenario: RepositoryScenario,
        number: int,
        body: dict[str, object] | None,
    ) -> dict[str, object]:
        pull_request = self._require_bound(scenario, number)
        if body != {"sha": pull_request.head_sha, "merge_method": "merge"}:
            raise AssertionError("qualification landing body escaped the captured head")
        if pull_request.state != "OPEN":
            raise AssertionError("qualification landing attempted a terminal pull request")
        prior_base = scenario.base_sha
        result_sha = fixture_sha(f"{scenario.repository}:landed:{number}")
        candidate_result = scenario.commits[
            fixture_sha(f"{scenario.repository}:candidate:{number}")
        ]
        scenario.commits[result_sha] = (candidate_result[0], (prior_base, pull_request.head_sha))
        scenario.base_sha = result_sha
        scenario.base_tree_sha = candidate_result[0]
        pull_request.state = "MERGED"
        pull_request.merge_commit_sha = result_sha
        pull_request.base_sha_at_merge = prior_base
        return {"merged": True, "sha": result_sha}

    @staticmethod
    def _reconciliation_pull_request(
        scenario: RepositoryScenario, pull_request: PullRequestState
    ) -> dict[str, object]:
        return {
            "number": pull_request.number,
            "state": "closed" if pull_request.state == "MERGED" else pull_request.state.lower(),
            "merged": pull_request.state == "MERGED",
            "merge_commit_sha": pull_request.merge_commit_sha,
            "head": {
                "sha": pull_request.head_sha,
                "ref": f"pr-{pull_request.number}",
                "repo": {"id": scenario.repository_id},
            },
            "base": {
                "sha": pull_request.base_sha_at_merge or scenario.base_sha,
                "ref": scenario.base_branch,
                "repo": {"id": scenario.repository_id},
            },
        }

    def _contains(self, scenario: RepositoryScenario, descendant: str, ancestor: str) -> bool:
        pending = [descendant]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == ancestor:
                return True
            if current in visited:
                continue
            visited.add(current)
            commit = scenario.commits.get(current)
            if commit is not None:
                pending.extend(commit[1])
        return False

    def _repository_identity(self, scenario: RepositoryScenario) -> dict[str, object]:
        return {
            "databaseId": scenario.repository_id,
            "nameWithOwner": scenario.repository,
            "owner": {"databaseId": scenario.owner_id},
        }

    def _pull_request_payload(
        self, scenario: RepositoryScenario, pull_request: PullRequestState, *, detailed: bool
    ) -> dict[str, object]:
        identity = {
            "databaseId": scenario.repository_id,
            "nameWithOwner": scenario.repository,
        }
        result: dict[str, object] = {
            "number": pull_request.number,
            "headRefOid": pull_request.head_sha,
            "baseRefOid": pull_request.base_sha_at_merge or scenario.base_sha,
            "baseRefName": scenario.base_branch,
            "updatedAt": "2026-09-09T00:00:00Z",
            "state": pull_request.state,
            "mergeCommit": (
                None
                if pull_request.merge_commit_sha is None
                else {"oid": pull_request.merge_commit_sha}
            ),
            "headRef": None
            if pull_request.state != "OPEN"
            else {"name": f"pr-{pull_request.number}"},
            "headRepository": identity,
            "baseRepository": identity,
        }
        if detailed:
            result.update(
                url=f"https://example.test/{scenario.repository}/pull/{pull_request.number}",
                title=f"Qualification change {pull_request.number}",
                createdAt="2026-09-08T00:00:00Z",
                isDraft=False,
                headRefName=(None if pull_request.state != "OPEN" else f"pr-{pull_request.number}"),
                authorAssociation="MEMBER",
                author={
                    "__typename": "User",
                    "databaseId": pull_request.author_id,
                    "login": pull_request.author_login,
                },
                labels=self._connection([{"name": "queue"}]),
            )
        return result

    def _requested_pull_requests(
        self, scenario: RepositoryScenario, variables: Mapping[str, object]
    ) -> tuple[PullRequestState, ...]:
        requested: list[PullRequestState] = []
        index = 0
        while f"number{index}" in variables:
            raw = variables[f"number{index}"]
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise AssertionError("qualification GraphQL PR number is malformed")
            requested.append(self._require_bound(scenario, raw))
            index += 1
        if len(requested) != 2:
            raise AssertionError("qualification GraphQL did not use the exact two-PR scope")
        return tuple(requested)

    def _require_graphql_scope(
        self, scenario: RepositoryScenario, variables: Mapping[str, object]
    ) -> None:
        owner, name = scenario.repository.split("/", 1)
        if variables.get("owner") != owner or variables.get("name") != name:
            raise AssertionError("qualification GraphQL escaped its repository")
        if variables.get("base") != f"refs/heads/{scenario.base_branch}":
            raise AssertionError("qualification GraphQL escaped its base branch")

    def _require_bound(self, scenario: RepositoryScenario, number: int) -> PullRequestState:
        pull_request = scenario.pull_requests.get(number)
        if pull_request is None or not pull_request.bound:
            raise AssertionError("qualification provider request addressed an unbound PR")
        return pull_request

    @staticmethod
    def _path_pull_request_number(path: str) -> int:
        return int(path.split("/pulls/", 1)[1].split("/", 1)[0])

    @staticmethod
    def _connection(nodes: list[dict[str, object]]) -> dict[str, object]:
        return {
            "totalCount": len(nodes),
            "pageInfo": {"hasNextPage": False},
            "nodes": nodes,
        }

    def _commit_payload(
        self,
        scenario: RepositoryScenario,
        sha: str,
        *,
        checks: bool = False,
        parents: bool = False,
    ) -> dict[str, object]:
        tree, parent_shas = self._require_commit(scenario, sha)
        result: dict[str, object] = {"oid": sha, "tree": {"oid": tree}}
        if checks:
            result["statusCheckRollup"] = {
                "contexts": self._connection(
                    [
                        {
                            "__typename": "CheckRun",
                            "name": "required",
                            "status": "COMPLETED",
                            "conclusion": "SUCCESS",
                            "checkSuite": {"app": {"databaseId": 100}},
                        }
                    ]
                )
            }
        if parents:
            result["parents"] = self._connection(
                [{"oid": parent_sha} for parent_sha in parent_shas]
            )
        return result

    @staticmethod
    def _protection() -> dict[str, object]:
        return {
            "requiresStatusChecks": True,
            "requiresStrictStatusChecks": False,
            "requiredStatusChecks": [{"context": "required", "app": {"databaseId": 100}}],
        }

    @staticmethod
    def _comparison(base_sha: str, head_sha: str, status: str) -> dict[str, object]:
        return {
            "status": status,
            "baseTarget": {"oid": base_sha},
            "headTarget": {"oid": head_sha},
        }

    @staticmethod
    def _require_commit(scenario: RepositoryScenario, sha: str) -> tuple[str, tuple[str, ...]]:
        try:
            return scenario.commits[sha]
        except KeyError as error:
            raise AssertionError(f"qualification provider lacks commit {sha}") from error

    def _require_context(self) -> QualificationContext:
        context = self._context.get()
        if context is None:
            raise AssertionError("qualification provider call lacks worker/request context")
        return context

    def _begin_call(
        self,
        *,
        context: QualificationContext,
        scenario: RepositoryScenario,
        method: str,
        path: str,
        body: dict[str, object] | None,
        phase: str,
        authority_kind: str,
        authority_id: int,
    ) -> ProviderCall:
        started = self.clock.monotonic()
        call = ProviderCall(
            sequence=len(self.calls) + 1,
            worker_id=context.worker_id,
            request_id=context.request_id,
            repository=scenario.repository,
            authority_kind=authority_kind,
            authority_id=authority_id,
            method=method,
            path=path,
            phase=phase,
            resource_class=ordinary_provider_resource_class(path),
            started_at=started,
            body=body,
        )
        self.calls.append(call)
        self.clock.record_provider_call()
        return call

    def _finish_call(
        self,
        call: ProviderCall,
        *,
        graphql_cost: int = 0,
        error: Exception | None = None,
    ) -> None:
        call.graphql_cost = graphql_cost
        call.completed_at = self.clock.monotonic()
        call.outcome = "error" if error is not None else "success"
        call.error_type = type(error).__name__ if error is not None else None

    def _raise_injected_failure(self, call: ProviderCall, *, after_dispatch: bool) -> None:
        for index, failure in enumerate(self._failures):
            if (
                failure.repository == call.repository
                and failure.phase == call.phase
                and failure.after_dispatch == after_dispatch
            ):
                self._failures.pop(index)
                raise failure.error

    def requested_pull_request_numbers(self) -> set[int]:
        numbers: set[int] = set()
        for call in self.calls:
            body = call.body or {}
            variables = body.get("variables")
            if isinstance(variables, dict):
                numbers.update(
                    value
                    for key, value in variables.items()
                    if key.startswith("number") and isinstance(value, int)
                )
            if "/pulls/" in call.path:
                numbers.add(self._path_pull_request_number(call.path))
        return numbers

    def expected_permissions_for(self, effect_profile: str) -> tuple[str, ...]:
        return ordinary_agent_effect_permissions(effect_profile)

    @property
    def active_token_count(self) -> int:
        return len(self._tokens)
