These are immutable source fixtures, used to execute native config parsing and
addon discovery against temporary addon files. They are not installed Odoo or
live-lane evidence. No network, Docker, Odoo registry, real user or invitation
is involved.

`config.py.txt`, `module.py.txt` and `release.py.txt` are unmodified Odoo sources
from commit `41f91eb726dd354dbbf193e724fce4e4d9a34971` (20.0).
`func.py.txt` is that revision's unchanged `classproperty` definition.
Source: https://github.com/odoo/odoo/tree/41f91eb726dd354dbbf193e724fce4e4d9a34971/odoo
Copyright Odoo S.A.; LGPL-3, reproduced in LICENSE.

`startup.py.txt` contains unchanged startup settings/bootstrap definitions and
constants selected from `docker/scripts/run_odoo_startup.py` in odoo-devkit
commit `dfcc52d6aec24cac02bf9574386a190472106fe3`, with their imports.
`odoo_addon_paths.py.txt` and `odoo_admin_password.py.txt` are unchanged sibling
sources from that revision. Source and LGPL-3 license:
https://github.com/cbusillo/odoo-devkit/tree/dfcc52d6aec24cac02bf9574386a190472106fe3/docker/scripts

The tests supply package shells and inert paths for unrelated platform,
translation and ORM dependencies. The startup config writer, ConfigManager,
initialize_sys_path and Manifest run their real code. Only startup's default
data directory is relocated into the temporary directory; ODOO_DATA_DIR stays
unset to distinguish generated effective config from native per-user defaults.
SQL eligibility is exercised through the existing in-memory database adapter.
