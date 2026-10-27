# Historical worker compatibility

Every Code and Codex Lab execution are retired by [DIRECTION.md](../DIRECTION.md).
The runner, polling daemon, session launchers, completion callbacks and local
reconciliation commands are removed. This source deletion does not stop host
processes or remove stored records, saved session state, worktrees or branches.

The historical API adapter retains authenticated work-request reads and the
transport used by [advisory engineering review](engineering-review-runs.md).
`launchplane every-code review-run-once` remains its supported CLI entrypoint.
Review execution still requires the service-authorized absolute executable
path and SHA-256; no executable authority or credential is changed here.

Historical request IDs, workspace paths and branch names stay stable for record
reads, advisory review and existing preview matching. The existing worker and
GitHub credential names remain because shared service consumers still use them.
No missing record or closure evidence authorizes workspace or data deletion.

## Remote session contract

The historical Codex Lab session client used the Discord Blue
[remote agent session contract](https://github.com/cbusillo/discord-blue/blob/main/docs/agent-session-protocol.md).
It connects to `/agent-session/connect`; Launchplane does not implement the
WebSocket client. The removed worker executable does not establish current DUI
connectivity.


Discord Blue testing qualification remains a separate held workstream; this
worker deletion neither changes its bridge nor qualifies its testing lane.
