# Security Policy

## Supported Versions

Security fixes target the current `main` branch. There are no separate
supported release lines.

## Reporting a Vulnerability

Report suspected vulnerabilities privately through GitHub's
[Report a vulnerability](https://github.com/cbusillo/launchplane/security/advisories/new)
form. Do not open a public issue for a vulnerability.

Include the commit, the affected API, workflow, or UI page, the impact, and
the smallest steps that reproduce it.

Do not send secrets, tokens, database contents, customer data, real domain
or host names, or other personal data. Use redacted or made-up values.

This is a single-maintainer project. Reports are handled on a best-effort
basis, and I aim to reply within seven days.

## Scope

Relevant reports include:

- merging, deploying, or promoting a change without the required admin
  permission or Client acceptance;
- managed secrets or credentials leaking through the API, the Launchplane
  UI, logs, or records;
- changing or hiding release, deployment, or audit records;
- preview environments exposing data or access they should not; and
- dependency, container image, or GitHub Actions supply-chain problems.

Problems in GitHub, Dokploy, or a deployed product's own code should go to
that project.
