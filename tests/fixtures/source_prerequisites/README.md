`override_payload.py.txt` is an immutable source artifact from
`odoo-shared-addons` PR80, landing
`4d935ec74954dc4848d76a77808b5c693e238e9c`, file
`launchplane_settings/services/override_payload.py`.

The tests stage its exact bytes in a synthetic installed addon and bind them to
the fixture artifact inventory. This exercises the shipped parser without
importing Odoo or reading a sibling checkout. The connection producer alongside
it has deliberately synthetic defaults and caller shapes.
