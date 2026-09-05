# Codex Lab worker runtime

Launchplane's interactive work-request and PR-feedback sessions launch
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

Codex Lab's session client maps this provenance into the Discord Blue
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
