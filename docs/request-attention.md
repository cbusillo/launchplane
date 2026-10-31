---
title: Waiting requests
---

The administrator page at `/ui/engineering/request-attention` reads waiting work
from its original source records. It shows the source link, product when mapped,
requester login, recorded agent, original source age and next action. Unassigned work is grouped
under Supervisor for the next routine check. This is a view, not an autonomous
dispatcher, a permission to start work, or a release decision.

Requests remain visible while their original issue or change is open. Replies,
acknowledgements, reactions and collector watermarks do not clear them. The
existing intake routine triages incoming comments, including Client-agent and
inline-review comments, onto the original work's Current Status. Imported
TestFlight feedback is already an ordinary source issue. This page introduces
no TestFlight or Discord collector.

Under the catalog intake rule, an agent must verify a real new request on closed
work, then reopen its source issue. A merged change that
cannot reopen reuses or gets one issue, with the source comment link and next
action until done ([Q1105](https://github.com/cbusillo/launchplane/issues/5405#issuecomment-6537114111)).
The existing intake agent verifies that it is a real request before reopening;
acknowledgements alone do not qualify. The weekly audit must report sweep misses
and requests left waiting together with false reopens and observed time and
tool cost per request, using existing source/Supervisor evidence. It states
missing measurements rather than inventing efficiency or effectiveness. These
are [catalog intake](https://github.com/cbusillo/codex-skills/blob/main/skills/github-plan/SKILL.md#triage-incoming-reports)
and weekly-audit duties, not a new collector or dispatcher in this page.

`GET /v1/attention/requests` returns source inventory without contacting the
source-control provider. Add `repository=OWNER/REPOSITORY` to read open work. The adapter follows
source cursors within one repository read; `truncated` explicitly reports the
read bound. An incomplete discussion or failed cursor is a coverage gap. Responses use Launchplane terms (`request`, `change`,
`source_id`), and only the GitHub adapter knows the current provider endpoints.
The page deduplicates source IDs and folds changes into their open canonical requests
in the same repository and reads up to four repositories
at a time. Refresh re-reads source state, and navigation cancels outstanding
browser reads. Failed sources remain visible as coverage gaps. A denied refresh
clears previous results through the existing Engineering Ops resource lifecycle.

Coverage is limited to tracked repository inventory, with unmapped products'
repository links shown as unavailable sources. The page explicitly states that
sources outside this inventory are not covered. It lists every open work item
in that scope, including agent-filed work with a remaining next action. Missing
or ambiguous Current Status routes to Supervisor triage rather than inventing an
assignment or hiding the request. The existing shared planning package resolves trusted body/discussion status and
authored releases; the page does not copy claim or release grammar. Existing
repository policy supplies trusted automation IDs. A recorded abandoned-session
release requires source verification and is routed to Supervisor, since this
read does not qualify termination evidence. Person and event waits keep their
recorded next actor and are excluded from the unowned filter. A general
`agent` next actor or a released worker's historical claim is not an assigned
agent. A later source edit that prevents verifying recorded status keeps the
request visible with a source-verification action. The Opened field measures
source creation age, including after reopening, rather than claiming how long a
late request has waited. Product mappings come from existing profiles. Requester logins remain
source metadata and do not create a new person or Client-agent identity map.

The original sources are the only durable request store. Existing collection
remains responsible for discovery and triage. The page reads the resulting open
records live rather than using the comment radar's reply-based attention filter
or publishing watcher notices to a second store. No snapshot is persisted in
Launchplane, and no source comment, issue, approval or runtime record is changed.

Access requires the existing repository inventory read permission and an
administrator or Director-agent identity. Product Clients and read-only human
sessions cannot read the cross-product view. Provider reads reuse Launchplane's
existing repository-read token purpose, restricted to read permissions, and token
lifetime management. One token covers each repository read. This adds no
credential, grant or permission system.

## Message events

The existing signed App webhook receiver also publishes identifier-only message
hints for tracked repository inventory. It uses the inventory's canonical
repository name. Openings, body edits, conversation comments, inline comments
and review bodies wake existing readers. No source body, new credential,
second public webhook receiver or durable request copy is introduced.

Administrators and Director agents with the existing attention read permission
can subscribe at `GET /v1/attention/requests/events`. The bounded JSON-lines
response sends connection and heartbeat observations and message identifiers.
It uses a dedicated PostgreSQL connection, releases it on disconnect, limits
concurrent subscribers, and reconnects within a minute to recheck access.
The catalog's existing foreign watcher consumes these hints, rereads source,
and uses its existing native-session delivery. The attention view continues
to read the original waiting requests; event delivery never clears a request.

The App must subscribe to issues, pull requests, issue comments, review comments
and review submissions. Existing grants and event subscriptions are separate:
source delivery does not assert live activation. Any needed access or credential
change remains a Director decision. The scheduled watch/radar sweep recovers
missed transient notifications and records causes on its existing repair issue.
