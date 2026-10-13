from contextlib import contextmanager
from dataclasses import replace
from functools import wraps
from inspect import iscoroutinefunction
from collections.abc import Awaitable, Callable, Iterator
from typing import ParamSpec, TypeVar, cast
from unittest.mock import patch

from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubActionsIdentity,
    LaunchplaneAuthzPolicy,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    LocalOperatorPolicyRule,
    TerminalAgentIdentity,
    bearer_identity_from_token,
    configured_local_operator_identity,
    configured_terminal_agent_identity,
)


@contextmanager
def scoped_agent_transport() -> Iterator[None]:
    """Model a future Client transport with existing token-bound policy principals.

    Authenticate the synthetic token normally, but do not mark it as the
    Director's bootstrap identity. Production Client credentials are not enabled.
    The endpoint, returned-product checks and grants remain real.
    """

    def scoped_bearer(
        *, token: str, config: BearerIdentityConfig
    ) -> LocalOperatorIdentity | TerminalAgentIdentity | LocalAdminIdentity | None:
        identity = bearer_identity_from_token(token=token, config=config)
        if isinstance(identity, LocalOperatorIdentity | TerminalAgentIdentity):
            return replace(identity, director_agent=False)
        return identity

    def scoped_operator(config: BearerIdentityConfig) -> LocalOperatorIdentity | None:
        identity = configured_local_operator_identity(config)
        return replace(identity, director_agent=False) if identity is not None else None

    def scoped_terminal(config: BearerIdentityConfig) -> TerminalAgentIdentity | None:
        identity = configured_terminal_agent_identity(config)
        return replace(identity, director_agent=False) if identity is not None else None

    with (
        patch("control_plane.http_app.bearer_identity_from_token", side_effect=scoped_bearer),
        patch("control_plane.http_app.configured_local_operator_identity", scoped_operator),
        patch("control_plane.http_app.configured_terminal_agent_identity", scoped_terminal),
    ):
        yield


_P = ParamSpec("_P")
_R = TypeVar("_R")


def scoped_agent_test(test: Callable[_P, _R]) -> Callable[_P, _R]:
    """Retain scoped-agent regression tests separately from Director-agent tests."""
    if iscoroutinefunction(test):
        async_test = cast(Callable[_P, Awaitable[object]], test)

        @wraps(test)
        async def run_async(*args: _P.args, **kwargs: _P.kwargs) -> object:
            with scoped_agent_transport():
                return await async_test(*args, **kwargs)

        return cast(Callable[_P, _R], run_async)

    @wraps(test)
    def run(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with scoped_agent_transport():
            return test(*args, **kwargs)

    return run


class StubVerifier:
    def __init__(self, verified_identity: GitHubActionsIdentity):
        self.identity = verified_identity

    def verify(self, token: str) -> GitHubActionsIdentity:
        if token != "valid-token":
            raise ValueError("OIDC bearer token is required.")
        return self.identity


def identity(
    *,
    repository: str = "every/verireel",
    workflow_ref: str = "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main",
    job_workflow_ref: str = "",
    event_name: str = "pull_request",
    ref: str = "refs/heads/main",
    environment: str = "",
    repository_id: str = "1001",
    repository_owner_id: str = "2001",
) -> GitHubActionsIdentity:
    return GitHubActionsIdentity(
        repository=repository,
        repository_owner=repository.split("/", 1)[0],
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        workflow_ref=workflow_ref,
        job_workflow_ref=job_workflow_ref,
        ref=ref,
        ref_type="branch",
        event_name=event_name,
        environment=environment,
        subject="repo:every/verireel:pull_request",
        sha="6b3c9d7e8f901234567890abcdef1234567890ab",
        raw_claims={
            "repository": repository,
            "workflow_ref": workflow_ref,
            "run_id": "1001",
            "run_attempt": "1",
        },
    )


_StubVerifier = StubVerifier
_identity = identity


def local_operator_policy(
    *,
    actions: tuple[str, ...],
    products: tuple[str, ...] = ("*",),
    contexts: tuple[str, ...] = ("*",),
    subject: str = "local-owner-agent",
    token_label: str = "local-owner-write",
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy(
        local_operators=(
            LocalOperatorPolicyRule(
                subjects=(subject,),
                token_labels=(token_label,),
                products=products,
                contexts=contexts,
                actions=actions,
            ),
        )
    )


_local_operator_policy = local_operator_policy
