"""Project public HTML to a passive document; never reuse the site's scripts."""

import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from control_plane.contracts.prepared_public_site import PublicSitePlan, validate_public_path

# These are content grammar, not product route configuration.
DISPLAY_TAGS = frozenset(
    "html head title body main header footer nav section article aside div span p a "
    "h1 h2 h3 h4 h5 h6 ul ol li dl dt dd table thead tbody tfoot tr th td caption "
    "strong em b i u s small sub sup blockquote pre code br hr img picture source "
    "video audio track figure figcaption link style details summary address time".split()
)
VOID_TAGS = frozenset({"br", "hr", "img", "source", "track", "link"})
DROP_TAGS = frozenset({"script", "iframe", "object", "embed", "template", "form", "textarea"})
SAFE_ATTRIBUTES = frozenset(
    "id class title lang dir role alt width height colspan rowspan scope datetime "
    "controls loop muted preload loading decoding media type rel sizes label kind".split()
)
URL_ATTRIBUTES = frozenset({"src", "href", "poster"})
NOTICE_ID = "launchplane-public-pause"


def local_reference(plan: PublicSitePlan, base: str, value: str) -> str | None:
    value = value.strip()
    if not value or value.startswith("#"):
        return None
    parsed = urlsplit(urljoin(plan.origin + base, value))
    if f"{parsed.scheme}://{parsed.netloc}" != plan.origin:
        raise ValueError("referenced resources must be local to the declared origin")
    return validate_public_path(urlunsplit(("", "", parsed.path or "/", parsed.query, "")))


def css_references(plan: PublicSitePlan, base: str, text: str) -> set[str]:
    # Escapes and comments can conceal URL syntax; unsupported CSS fails preparation.
    if "\\" in text or "/*" in text or re.search(r"@import|expression\s*\(", text, re.I):
        raise ValueError("CSS requires a plain, explicit local resource closure")
    refs = set()
    for match in re.finditer(r"url\(\s*(['\"]?)(.*?)\1\s*\)", text, re.I):
        value = match.group(2)
        if value.startswith("data:"):
            continue
        ref = local_reference(plan, base, value)
        if ref:
            refs.add(ref)
    if len(re.findall(r"url\s*\(", text, re.I)) != len(
        re.findall(r"url\(\s*(['\"]?)(.*?)\1\s*\)", text, re.I)
    ):
        raise ValueError("unsupported CSS URL syntax")
    return refs


class PassivePublicHTML(HTMLParser):
    def __init__(self, plan: PublicSitePlan, path: str) -> None:
        super().__init__(convert_charrefs=True)
        self.plan = plan
        self.path = path
        self.parts: list[str] = []
        self.assets: set[str] = set()
        self.links: set[str] = set()
        self.dropped: list[str] = []
        self.stack: list[str] = []
        self.in_style = False
        self.saw_body = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.dropped:
            if tag in DROP_TAGS:
                self.dropped.append(tag)
            return
        values = dict(attrs)
        if tag in DROP_TAGS:
            if tag == "script" and values.get("src"):
                ref = local_reference(self.plan, self.path, str(values["src"]))
                if ref:
                    self.assets.add(ref)
            if tag == "form":
                self.parts.append('<p role="status">Forms temporarily paused.</p>')
                self.parts.append('<button type="button" disabled>Submissions paused</button>')
            if tag not in {"embed"}:
                self.dropped.append(tag)
            return
        if tag in {"input", "button", "meta"}:
            return
        if tag not in DISPLAY_TAGS:
            raise ValueError(f"unsupported public markup: {tag}")
        safe: list[tuple[str, str]] = []
        for name, value in attrs:
            if value is None:
                value = ""
            if name == "style":
                self.assets.update(css_references(self.plan, self.path, value))
                safe.append((name, value))
            elif name == "srcset":
                entries = []
                for entry in value.split(","):
                    pieces = entry.strip().split()
                    if not pieces:
                        continue
                    ref = local_reference(self.plan, self.path, pieces[0])
                    if ref:
                        self.assets.add(ref)
                        entries.append(" ".join([ref, *pieces[1:]]))
                safe.append((name, ", ".join(entries)))
            elif name in URL_ATTRIBUTES:
                if tag == "a":
                    if value.startswith(
                        ("https://", "http://", "mailto:", "tel:")
                    ) and not value.startswith(self.plan.origin + "/"):
                        safe.append((name, value))
                        continue
                    ref = local_reference(self.plan, self.path, value)
                    if ref and self.plan.excluded(ref):
                        safe.extend([("aria-disabled", "true"), ("title", "Temporarily paused")])
                        continue
                    if ref:
                        self.links.add(ref)
                else:
                    ref = local_reference(self.plan, self.path, value)
                    if ref:
                        self.assets.add(ref)
                safe.append((name, ref or value))
            elif name in SAFE_ATTRIBUTES or name.startswith("aria-"):
                safe.append((name, value))
        if tag == "link" and values.get("rel") not in {"stylesheet", "icon"}:
            return
        self.parts.append(
            "<" + tag + "".join(f' {n}="{escape(v, quote=True)}"' for n, v in safe) + ">"
        )
        if tag not in VOID_TAGS:
            self.stack.append(tag)
        if tag == "style":
            self.in_style = True
        if tag == "body":
            self.saw_body = True
            self.parts.append(
                f'<aside id="{NOTICE_ID}" role="status" style="display:block!important;position:relative!important;'
                "background:#fff4c2!important;color:#241c00!important;padding:16px!important;"
                'font:18px sans-serif!important">' + escape(self.plan.notice) + "</aside>"
            )

    def handle_endtag(self, tag: str) -> None:
        if self.dropped:
            if tag == self.dropped[-1]:
                self.dropped.pop()
            return
        if tag in {"input", "button", "meta", "embed"}:
            return
        if tag in VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            raise ValueError("public HTML must have an explicit balanced structure")
        self.stack.pop()
        if tag == "style":
            self.in_style = False
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.dropped:
            if self.in_style:
                self.assets.update(css_references(self.plan, self.path, data))
                self.parts.append(data)
            else:
                self.parts.append(escape(data))

    def render(self, text: str) -> str:
        self.feed(text)
        self.close()
        if self.stack or self.dropped or not self.saw_body:
            raise ValueError("public HTML is incomplete")
        return "<!doctype html>" + "".join(self.parts)
