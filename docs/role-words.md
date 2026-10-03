# Role Words

Launchplane's docs and UI use the shared role words defined in the skills
catalog's
[`skills/references/role-words.md`](https://github.com/cbusillo/codex-skills/blob/main/skills/references/role-words.md).
They name a role, never a particular person.

- **Director**: the person whose `DIRECTION.md` the agents follow. Agents ask
  the Director at a stop boundary.
- **Client**: the person whose business a product serves. The Client's
  acceptance releases that product to production. Launchplane's release
  review asks the product's Client.
- **admin**: a permission, not a role. It covers what Launchplane formerly
  called the operator or policy administrator: granting access, changing who
  can merge, protected admin workflows, and audited control-plane writes. The
  Director normally holds it.
- **repository owner**: GitHub's literal sense only, the user or organization
  in `OWNER/REPO`, owner-only GitHub settings, and code-owner review.

"Operator" and "owner" are not role words. In prose, the Launchplane web UI is
the Launchplane UI, a protected GitHub workflow for audited writes is an admin
workflow, and an approval recorded without the Client is an admin override.
Technical holders take a plain word: a lease holder, the party that manages
TLS.

## Release Rule

This is the target rule; it lands with the code migration (cbusillo/direction#13
step (d) and cbusillo/launchplane#2716). Until then, release review requires a
recorded Client acceptance or admin override, as
[release-review.md](release-review.md) describes, even when the Client is the
Director.

Each product records its Client. A production release needs the Client's
acceptance, unless the Client is the Director; then the Director's standing
direction is the acceptance. Either way the release runs the same gated path:
verified backup, release record, post-deploy checks, automatic rollback.

## Legacy Identifiers

Code identifiers, routes, API fields, stored fields, file names, and command
names written before these words keep their spelling until they are migrated
together with their readers (cbusillo/direction#13, step (d)). Prose quotes
them as code and describes their meaning in these words.

| Legacy identifier | Meaning |
| --- | --- |
| `profile.owner`, `owner_github_login`, `owner_github_id`, `owner_id` | the product's Client |
| `owner_review*`, `owner_acceptance_events`, `/owner-review` | the Client's review and acceptance |
| `owner_authority_*`, `ProductOwnerAuthority*` | the Client's authority over their product |
| `owner-control`, `owner_control_*` | the admin's trusted-host confirmation channel; not the Client |
| `owner_agent_identity`, `omit_owner_agent_env` | the Director's own agent and its write credentials |
| identity role `owner` | a signed-in Client |
| `can_override`, `overridden` release decisions | an admin override of the Client's review |
| `require_policy_administrator`, `policy_administrator*` | the admin permission |
| `operator_contract`, `agent-operator-contract`, `operator_bearer_config`, `local_operator`, `LAUNCHPLANE_OPERATOR_URL` | admin access and its contract |
| `docs/owner-acceptance.md`, `docs/owner-control-channel.md`, `docs/operator-experience.md`, `docs/agent-operator-contract.md`, `docs/product-owner-policy.md` | doc file names; their content uses these words |
| `## Owner test notes`, `Nothing for the owner to test`, `.github/actions/owner-test-notes` | older spellings of the `## Client test notes` section and its `Nothing for the Client to test` marker, still read; the action keeps its path because product repositories pin it |
| `launchplane/owner-review` status context, `<!-- launchplane:owner-review -->` comment marker, `owner-review` label | names GitHub and product repositories match on; the text around them says Client |
| release-record issue heading `## Owner checklist`; checklist text `Operator review is required.` | kept: retry recovery matches the issue body exactly, and the checklist text is part of `checklist_digest` |

`scripts/validate_role_words.py` fails when the old role words return in
tracked Markdown prose or in text the frontend shows people.
