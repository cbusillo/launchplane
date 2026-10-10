"""Validate passive SVG image resources and their local dependency closure."""

from xml.etree import ElementTree

from control_plane.contracts.prepared_public_site import PublicSitePlan
from control_plane.prepared_public_html import css_references, local_reference


def prepare_svg(plan: PublicSitePlan, path: str, body: bytes) -> tuple[bytes, set[str]]:
    if b"<!doctype" in body.lower() or b"<!entity" in body.lower():
        raise ValueError("SVG document/entity declarations are unsupported")
    root = ElementTree.fromstring(body)
    if root.tag != "{http://www.w3.org/2000/svg}svg":
        raise ValueError("SVG asset requires the SVG root namespace")
    refs: set[str] = set()
    for element in root.iter():
        name = element.tag.rsplit("}", 1)[-1].lower()
        if name in {"script", "foreignobject", "iframe", "object", "embed"}:
            raise ValueError("executable SVG cannot enter public copy")
        for attribute, value in list(element.attrib.items()):
            attribute_name = attribute.rsplit("}", 1)[-1].lower()
            if attribute_name.startswith("on"):
                raise ValueError("executable SVG attributes cannot enter public copy")
            if attribute_name.startswith("data-"):
                del element.attrib[attribute]
            elif attribute_name == "href" and not value.lower().startswith("data:"):
                ref = local_reference(plan, path, value)
                if ref:
                    refs.add(ref)
            elif attribute_name == "style":
                refs.update(css_references(plan, path, value))
        if name == "style" and element.text:
            refs.update(css_references(plan, path, element.text))
    return ElementTree.tostring(root, encoding="utf-8"), refs
