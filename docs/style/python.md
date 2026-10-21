---
title: Python Style
---

- Use Python 3.13 features when they improve clarity.
- Prefer explicit types on public functions and stored models.
- Keep functions small and domain-shaped.
- Use `pydantic` models for persisted contract data.
- Avoid abbreviations and low-signal variable names.
- Prefer `pathlib.Path` over string path manipulation.
- Keep CLI commands thin; put real logic in package modules.
- Run the Ruff formatting check defined in `.github/github.json`; CI enforces it
  for both same-repository and fork pull requests.
- Follow the shared
  [Names Instead Of Comments](https://github.com/cbusillo/codex-skills/blob/main/skills/references/python-odoo-style.md#names-instead-of-comments)
  rule: no new docstrings, and no comments outside its exceptions. CI checks
  the lines each change adds with the shared `check_new_comments.py`.
