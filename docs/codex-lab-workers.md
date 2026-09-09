# Codex Lab worker runtime

The current Launchplane interactive work-request and PR-feedback worker launches
`codex-lab` from the worker's executable search path. There is no automatic
fallback to the retired Every Code `code` executable. A missing Codex Lab binary
fails the session and follows the existing fenced completion reporting path.
The default prompt preserves issue/comment inspection, isolated worktrees, PR
creation, validation, and completion reporting.

The optional `--command-template` is an explicit operator-supplied shell
override. Existing worker service arguments must be inspected at cutover:
remove a legacy override or replace it with the intended Codex Lab command.
Changing the default does not rewrite an already-running worker's arguments.
Feedback sessions use the built-in Codex Lab command, independently of the
initial work-request shell override.

This current worker choice does not make Codex Lab the only engineering client.
The reconciled target in issue `#2240` gives Codex CLI and Codex Lab
the same scoped Launchplane service path. Codex Lab release/runtime work remains
separate and is not an ordinary Codex CLI pilot prerequisite unless a
concrete dependency is demonstrated. Nothing in this target statement changes
the deployed worker command or grants a client new authority.

## Session provenance

The launcher exports only the generic session projection:

- `AGENT_SESSION_ORIGIN=launchplane`
- `AGENT_SESSION_SOURCE=agent-session`
- `AGENT_SESSION_REQUEST_ID`
- `AGENT_SESSION_REPOSITORY`
- `AGENT_SESSION_ISSUE_NUMBER`
- `AGENT_SESSION_ISSUE_URL`

The retired `EVERY_CODE_SESSION_ORIGIN`, `EVERY_CODE_REQUEST_ID`,
`EVERY_CODE_REPOSITORY`, `EVERY_CODE_ISSUE_NUMBER`, and `EVERY_CODE_ISSUE_URL`
assignments are no longer generated. Request IDs remain opaque durable record
identifiers; historical `every-code-` prefixes do not select an executable.

## Feedback workspace lifetime

When a request finishes with a result PR, the worker stops the completed
process but retains its worktree and saved session state for later PR feedback.
Feedback can arrive after intervening polling passes, so cleanup must preserve
the state needed to reuse the same request and worktree.
The retained worktree remains worker-owned and is not a workspace for manual
processes: terminal-process cleanup continues during polling.

A signed linked-PR close or merge event records closure even if execution
already finished. The closure marker prefixes the existing result summary;
it describes the PR, not the execution outcome. That metadata does not change
the completed execution's outcome, fencing token, or finish time. First
observed closure remains final for this request even if the PR is reopened.
Once closure is recorded, ordinary
cleanup may remove the saved workspace under its existing safety checks.
Terminal requests without a result PR remain eligible for cleanup. Dirty or
uninspectable worktrees continue to be protected independently.

Cleanup reconciliation also preserves workspaces awaiting PR feedback and
reports `awaiting_pull_request_feedback`. A missed closure event can therefore
retain disk state; absence of closure evidence is not permission to delete it.
For a missed event, an authorized repository operator can redeliver the
original GitHub webhook through GitHub's delivery interface. Launchplane still
validates the signed event and its request/PR match. Do not replace missing
closure evidence with a local record edit or manual deletion.

Workspace retention alone does not enable production feedback relaunch. The
current service status API rejects terminal-to-running transitions, while the
rerun API requires approved write-intent evidence. The local direct-store
feedback path does not prove that service-backed transition or a fresh fence.
A supported feedback-resume lifecycle must preserve those authorization and
fencing requirements; do not work around them with a local record write.

`tests/test_http_app_every_code_restart_boundary.py` exercises the real
FastAPI routes with lifespan handling and the SQL record store on SQLite.
It checks terminal restart rejection for both worker-token and update-scoped
callers, the separation of rerun authority from approved intent, and rejection
of old-fence or wrong-host callbacks after an authorized rerun and fresh claim.
Rejected writes must preserve the complete stored request. This is service
contract evidence, not PostgreSQL concurrency or positive feedback-resume
acceptance. The existing PostgreSQL integration gate separately covers claims,
heartbeats, stale recovery, status fencing, and completion racing linked-PR
closure.

The worker test module's `_EveryCodeApiHandler` is a transport fixture with
direct record writes; its successful reruns and feedback sessions do not prove
service authorization or atomic restart behavior. Direct-store delayed-feedback
tests establish workspace retention only. Issue
[#2328](https://github.com/cbusillo/launchplane/issues/2328) tracks the approved
implementation and remaining service/PostgreSQL acceptance matrix under
[#2058](https://github.com/cbusillo/launchplane/issues/2058).

The [feedback continuation foundation](every-code-feedback-resume.md) adds strict
contracts, isolated SQL evidence and inert launch protocol tests. It does not
wire the deployed worker or prove positive service-backed restart. A distinct
exact session handoff receipt and supported operator recovery remain mandatory
before production enablement.

The current Codex Lab session client maps this provenance into the Discord Blue
[remote agent session contract](https://github.com/cbusillo/discord-blue/blob/main/docs/agent-session-protocol.md).
It connects to `/agent-session/connect`; Launchplane does not implement the
WebSocket client. The worker executable cutover alone does not establish DUI
connectivity.

## Durable control-plane boundary

The `launchplane every-code` CLI namespace, work-request schemas, database
records, HTTP routes, existing worker credential names, tmux identifiers, and
worktree locations remain stable. They are historical control-plane identifiers,
not an instruction to run Every Code. Renaming them requires its own storage,
authorization, and client migration. Retain existing request IDs and fencing
tokens so outstanding work can be reconciled without duplicate claims.

Engineering-review jobs already consume a service-authorized absolute
executable path and SHA-256. At runtime cutover, their authority must identify
the intended Codex Lab binary and digest through the supported Launchplane
operator surface. Do not substitute the review binary in code, bypass its hash
check, or treat a repository change as proof that live authority was updated.

## Cutover verification

These checks qualify the Codex Lab-hosted worker lane. They do not gate an
ordinary Codex CLI delegated-delivery pilot unless that pilot actually uses
this worker runtime or another concrete dependency is recorded.

1. Inspect the current worker arguments and queued/claimed requests through
   the supported operational surfaces. Drain or reconcile existing leases;
   do not start a second worker to race the old owner.
2. Install and verify the intended Codex Lab binary on the worker host, with
   its own configured home and authentication. Do not copy Every Code state
   into Codex Lab's home as an implicit migration.
3. Deploy the worker change and replace any explicit legacy command override.
   Use Launchplane's supported runtime/operator path for managed deployments.
4. Verify one new request and one feedback relaunch use Codex Lab, preserve
   request/repository/issue provenance, and reach the correct fenced terminal
   record. Verify lease heartbeats separately from process launch success.
5. Once Codex Lab's built-in session client is available, verify Discord
   discovery, output, reply, pause, approvals, input, and reconnect against a
   real session. Successful unit tests do not establish this live result.
6. Verify engineering-review executable authority separately before enabling
   that lane, then retire any remaining Every Code launchers.
