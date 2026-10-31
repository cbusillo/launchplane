---
title: Waiting requests
---

The administrator page at `/ui/engineering/request-attention` reads waiting work
from its original source records. It shows the source link, product when mapped,
requester login, recorded agent, age and next action. Unassigned work is grouped
under Supervisor for the next routine check. This is a view, not an autonomous
dispatcher, a permission to start work, or a release decision.

Requests remain visible while their original issue or change is open. Replies,
acknowledgements, reactions and collector watermarks do not clear them. The
existing intake routine triages incoming comments, including Client-agent and
inline-review comments, onto the original work's Current Status. Imported
TestFlight feedback is already an ordinary source issue. This page introduces
no TestFlight or Discord collector.

`GET /v1/attention/requests` returns source inventory without contacting the
source-control provider. Add `repository=OWNER/REPOSITORY&page=1` to read an open
work page. `next_page` continues the same source; `truncated` explicitly reports
the page bound. Responses use Launchplane terms (`request`, `change`,
`source_id`), and only the GitHub adapter knows the current provider endpoints.
The page deduplicates source IDs across pages and reads up to four repositories
at a time. Refresh re-reads source state, and navigation cancels outstanding
browser reads. Failed sources remain visible as coverage gaps. A denied refresh
clears previous results through the existing Engineering Ops resource lifecycle.

Coverage is limited to tracked repository inventory, with unmapped products'
repository links shown as unavailable sources. The page explicitly states that
sources outside this inventory are not covered. It lists every open work item
in that scope, including agent-filed work with a remaining next action. Missing
or ambiguous Current Status routes to Supervisor triage rather than inventing an
assignment or hiding the request. The existing shared planning parser reads the
Next actor field; the page does not copy claim or release grammar. A general
`agent` next actor or a released worker's historical claim is not an assigned
agent. Product mappings come from existing profiles. Requester logins remain
source metadata and do not create a new person or Client-agent identity map.

The original sources are the only durable request store. Existing collection
remains responsible for discovery and triage. The page reads the resulting open
records live rather than using the comment radar's reply-based attention filter
or publishing watcher notices to a second store. No snapshot is persisted in
Launchplane, and no source comment, issue, approval or runtime record is changed.

Access requires the existing repository inventory read permission and an
administrator or Director-agent identity. Product Clients and read-only human
sessions cannot read the cross-product view. Provider reads reuse Launchplane's
existing delivery credential scope and token lifetime management. This adds no
credential, grant or permission system.
