---
title: Python Style
---

Read the full shared
[Python and Odoo style](https://github.com/cbusillo/codex-skills/blob/main/skills/references/python-odoo-style.md)
before writing Python. Odoo-specific rules apply only to Odoo; Launchplane's
supported Python versions and Ruff settings remain in its package metadata.

- Use `pydantic` models for persisted contract data.
- Keep CLI commands thin; put real logic in package modules.
- Run the Ruff formatting check defined in `.github/github.json`; CI enforces it
  for both same-repository and fork pull requests.
