# Role Words

Launchplane uses the catalog's [shared role words](https://github.com/cbusillo/codex-skills/blob/main/skills/references/role-words.md).

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
| `owner_agent_identity`, `omit_owner_agent_env` | the Director's own agent and its write credentials |
| identity role `owner` | a signed-in Client |
| `can_override`, `overridden` release decisions | an admin override of the Client's review |
| durable operation grant `policy_administrator`, reason code `authz_policy_administrator_quorum_unsatisfied` | the admin permission |
| `operator_contract`, `agent-operator-contract`, `operator_bearer_config`, `local_operator`, `LAUNCHPLANE_OPERATOR_URL` | admin access and its contract |
| `docs/owner-acceptance.md`, `docs/operator-experience.md`, `docs/agent-operator-contract.md` | doc file names; their content uses these words |
| `## Owner test notes`, `Nothing for the owner to test`, `.github/actions/owner-test-notes` | older spellings of the `## Client test notes` section and its `Nothing for the Client to test` marker, still read; the action keeps its path because product repositories pin it |
| `launchplane/owner-review` status context, `<!-- launchplane:owner-review -->` comment marker, `owner-review` label | names GitHub and product repositories match on; the text around them says Client |
| checklist text `Operator review is required.` | kept: the checklist text is part of `checklist_digest` |

`scripts/validate_role_words.py` fails when the old role words return in
tracked Markdown prose or in text the frontend shows people.
