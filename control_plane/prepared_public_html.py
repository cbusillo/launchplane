"""Project public HTML to a passive document; never reuse the site's scripts."""

from collections.abc import Sequence
from html import escape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import tinycss2
from tinycss2.ast import (
    AtKeywordToken,
    CurlyBracketsBlock,
    FunctionBlock,
    Node,
    ParenthesesBlock,
    ParseError,
    SquareBracketsBlock,
    StringToken,
    URLToken,
    WhitespaceToken,
)

from control_plane.contracts.prepared_public_site import PublicSitePlan, validate_public_path

# These are content grammar, not product route configuration.
DISPLAY_TAGS = frozenset(
    "html head title body main header footer nav section article aside div span p a "
    "h1 h2 h3 h4 h5 h6 ul ol li dl dt dd table thead tbody tfoot tr th td caption "
    "strong em b i u s small sub sup blockquote pre code br hr img picture source "
    "video audio track figure figcaption link style details summary address time label".split()
)
VOID_TAGS = frozenset({"br", "hr", "img", "source", "track", "link"})
DROP_TAGS = frozenset(
    {"script", "iframe", "object", "embed", "template", "textarea", "select", "button"}
)
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
    path = parsed.path or "/"
    return validate_public_path(path + ("?" + parsed.query if parsed.query else ""))


def css_references(plan: PublicSitePlan, base: str, text: str) -> set[str]:
    refs: set[str] = set()

    def add(value: str, *, imported: bool = False) -> None:
        if value.lower().startswith("data:") and not imported:
            return
        ref = local_reference(plan, base, value)
        if ref:
            refs.add(ref)

    def walk(nodes: Sequence[Node]) -> None:
        for index, node in enumerate(nodes):
            if isinstance(node, ParseError):
                raise ValueError("invalid CSS resource syntax")
            if isinstance(node, URLToken):
                add(node.value)
            elif isinstance(node, AtKeywordToken) and node.lower_value == "import":
                target = next(
                    (n for n in nodes[index + 1 :] if not isinstance(n, WhitespaceToken)), None
                )
                if isinstance(target, StringToken):
                    add(target.value, imported=True)
                elif isinstance(target, URLToken):
                    add(target.value, imported=True)
                elif isinstance(target, FunctionBlock) and target.lower_name == "url":
                    values = [n for n in target.arguments if not isinstance(n, WhitespaceToken)]
                    if len(values) != 1 or not isinstance(values[0], StringToken):
                        raise ValueError("invalid CSS import URL")
                    add(values[0].value, imported=True)
                else:
                    raise ValueError("unsupported CSS import target")
            elif isinstance(node, FunctionBlock):
                if node.lower_name == "url":
                    args = [n for n in node.arguments if not isinstance(n, WhitespaceToken)]
                    if len(args) != 1 or not isinstance(args[0], StringToken):
                        raise ValueError("invalid CSS URL function")
                    add(args[0].value)
                else:
                    if node.lower_name in {"image-set", "-webkit-image-set"}:
                        for argument in node.arguments:
                            if isinstance(argument, StringToken):
                                add(argument.value)
                    walk(node.arguments)
            elif isinstance(node, (CurlyBracketsBlock, ParenthesesBlock, SquareBracketsBlock)):
                walk(node.content)

    walk(tinycss2.parse_component_value_list(text, skip_comments=True))
    return refs


class PassivePublicHTML(HTMLParser):
    def __init__(self, plan: PublicSitePlan, path: str) -> None:
        super().__init__()
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
            if tag in DROP_TAGS and tag != "embed":
                self.dropped.append(tag)
            return
        values = dict(attrs)
        if tag == "form":
            self.parts.append('<p role="status">Forms temporarily paused.</p>')
            self.parts.append('<button type="button" disabled>Submissions paused</button>')
            return
        if tag in DROP_TAGS:
            if tag == "script" and values.get("src"):
                ref = local_reference(self.plan, self.path, str(values["src"]))
                if ref:
                    self.assets.add(ref)
            if tag not in {"embed"}:
                self.dropped.append(tag)
            return
        if tag == "meta":
            if str(values.get("charset", "")).lower() == "utf-8":
                self.parts.append('<meta charset="utf-8">')
            elif str(values.get("name", "")).lower() == "viewport" and values.get("content"):
                self.parts.append(
                    f'<meta name="viewport" content="{escape(str(values["content"]))}">'
                )
            return
        if tag in {"input", "button"}:
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
                    joined = urlsplit(urljoin(self.plan.origin + self.path, value))
                    origin = urlsplit(self.plan.origin)
                    if joined.scheme in {"mailto", "tel"} or (
                        joined.scheme in {"https", "http"}
                        and (joined.scheme, joined.netloc) != (origin.scheme, origin.netloc)
                    ):
                        safe.append((name, value))
                        continue
                    ref = local_reference(self.plan, self.path, value)
                    if ref and self.plan.excluded(ref):
                        safe.extend([("aria-disabled", "true"), ("title", "Temporarily paused")])
                        continue
                    if ref:
                        self.links.add(ref)
                    if ref and joined.fragment:
                        ref += "#" + joined.fragment
                else:
                    if tag in {"img", "source"} and value.lower().startswith("data:image/"):
                        safe.append((name, value))
                        continue
                    ref = local_reference(self.plan, self.path, value)
                    if ref:
                        self.assets.add(ref)
                safe.append((name, ref or value))
            elif name in SAFE_ATTRIBUTES or name.startswith("aria-"):
                safe.append((name, value))
        if tag == "link" and not set(str(values.get("rel", "")).lower().split()) & {
            "stylesheet",
            "icon",
            "apple-touch-icon",
            "apple-touch-icon-precomposed",
        }:
            return
        self.parts.append("<" + tag + "".join(f' {n}="{escape(v)}"' for n, v in safe) + ">")
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
        if tag in {"input", "button", "meta", "embed", "form"}:
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
