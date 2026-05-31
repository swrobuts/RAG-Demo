"""Convert raw MediaWiki HTML to Markdown plus a section tree.

The section tree is built by walking the HTML headings (h2..h4). Each section
collects the text that follows it until the next heading at the same or higher
level. We strip MediaWiki cruft (edit links, references list, infobox tables)
to keep the corpus focused on prose.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import markdownify

# Headings we treat as section boundaries. h1 is the article title (handled
# separately), h5/h6 are folded into their parent.
HEADING_LEVELS = {"h2": 2, "h3": 3, "h4": 4}

# Selectors to remove before extraction.
DROP_SELECTORS = [
    "table.infobox",
    "table.navbox",
    "table.metadata",
    "div.thumb",
    "div.hatnote",
    "div.reflist",
    "div.references",
    "ol.references",
    "sup.reference",
    "span.mw-editsection",
    "div.mw-references-wrap",
    "table.sidebar",
]


@dataclass
class CleanSection:
    level: int
    heading: str
    path: str          # "Geschichte > Gründung"
    text: str
    children: list["CleanSection"] = field(default_factory=list)


@dataclass
class CleanDocument:
    title: str
    markdown: str
    sections: list[CleanSection]  # top-level sections (level=2)


def _strip(soup: BeautifulSoup) -> None:
    for sel in DROP_SELECTORS:
        for el in soup.select(sel):
            el.decompose()


def _heading_level(tag: Tag) -> int | None:
    return HEADING_LEVELS.get(tag.name)


def _heading_text(tag: Tag) -> str:
    for edit in tag.select("span.mw-editsection"):
        edit.decompose()
    return tag.get_text(strip=True)


def _is_heading_wrapper(tag: Tag) -> bool:
    """Modern MediaWiki wraps each heading in
    ``<div class="mw-heading mw-headingN">…</div>``. Recognise that wrapper
    so we walk siblings at the right level."""
    if not isinstance(tag, Tag) or tag.name != "div":
        return False
    classes = tag.get("class") or []
    return any(c == "mw-heading" or c.startswith("mw-heading") for c in classes)


def _wrapper_heading_level(wrapper: Tag) -> int | None:
    """Get the heading level *of* a wrapper div (without descending into other
    wrappers)."""
    for child in wrapper.children:
        if isinstance(child, Tag):
            lvl = _heading_level(child)
            if lvl is not None:
                return lvl
    return None


def _effective_start(start: Tag) -> Tag:
    """Walk up from the heading to its mw-heading wrapper if present, so the
    ``.next_siblings`` walk happens at the right DOM level."""
    parent = start.parent
    if parent is not None and _is_heading_wrapper(parent):
        return parent
    return start


def _collect_following_text(start: Tag, stop_level: int) -> list[Tag | NavigableString]:
    """Collect siblings after ``start`` until a heading at ``stop_level`` or
    higher. Handles both the legacy MediaWiki output (flat headings) and the
    modern one (each heading inside its own ``<div class="mw-heading…">``)."""
    walker_start = _effective_start(start)
    out: list[Tag | NavigableString] = []
    for sib in walker_start.next_siblings:
        if isinstance(sib, Tag):
            # Wrapper-div containing the next heading
            if _is_heading_wrapper(sib):
                wlvl = _wrapper_heading_level(sib)
                if wlvl is not None and wlvl <= stop_level:
                    break
            # Bare heading (legacy HTML)
            lvl = _heading_level(sib)
            if lvl is not None and lvl <= stop_level:
                break
        out.append(sib)
    return out


def _nodes_to_markdown(nodes: list[Tag | NavigableString]) -> str:
    html = "".join(str(n) for n in nodes)
    md = markdownify(html, heading_style="ATX", bullets="-").strip()
    # Collapse runs of blank lines.
    lines = [ln.rstrip() for ln in md.splitlines()]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if not ln:
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(ln)
    return "\n".join(out).strip()


def clean_html(html: str, title: str) -> CleanDocument:
    soup = BeautifulSoup(html, "html.parser")
    _strip(soup)

    # Top-level container is normally a <div class="mw-parser-output">.
    root = soup.select_one("div.mw-parser-output") or soup

    # Walk headings in document order, build a stack-based tree.
    sections: list[CleanSection] = []
    stack: list[CleanSection] = []  # current path

    # Lead paragraphs: everything BEFORE the first heading marker among root's
    # direct children. The marker is either a bare heading (legacy HTML) or a
    # mw-heading wrapper (modern HTML).
    first_marker: Tag | None = None
    for child in root.children:
        if isinstance(child, Tag) and (
            _is_heading_wrapper(child) or _heading_level(child) is not None
        ):
            first_marker = child
            break

    lead_nodes: list[Tag | NavigableString] = []
    if first_marker is None:
        lead_md = _nodes_to_markdown(list(root.children))
        if lead_md:
            sections.append(CleanSection(level=2, heading="Einleitung", path="Einleitung", text=lead_md))
        markdown = f"# {title}\n\n{lead_md}".strip()
        return CleanDocument(title=title, markdown=markdown, sections=sections)

    for sib in root.children:
        if sib is first_marker:
            break
        lead_nodes.append(sib)
    lead_md = _nodes_to_markdown(lead_nodes)
    if lead_md:
        intro = CleanSection(level=2, heading="Einleitung", path="Einleitung", text=lead_md)
        sections.append(intro)
        stack = [intro]

    # Iterate headings in order.
    for tag in root.find_all(["h2", "h3", "h4"], recursive=True):
        lvl = _heading_level(tag)
        if lvl is None:
            continue
        heading = _heading_text(tag)
        if not heading or heading.lower() in {"weblinks", "literatur", "einzelnachweise"}:
            # Skip reference-y footer sections from the body of the article.
            continue

        following = _collect_following_text(tag, stop_level=lvl)
        text = _nodes_to_markdown(following)

        # Pop stack until parent has smaller level.
        while stack and stack[-1].level >= lvl:
            stack.pop()
        parent = stack[-1] if stack else None
        path = f"{parent.path} > {heading}" if parent else heading
        section = CleanSection(level=lvl, heading=heading, path=path, text=text)
        if parent is None:
            sections.append(section)
        else:
            parent.children.append(section)
        stack.append(section)

    # Build flat markdown for the document.
    def _walk(secs: list[CleanSection], buf: list[str]) -> None:
        for s in secs:
            buf.append("#" * s.level + " " + s.heading)
            if s.text:
                buf.append("")
                buf.append(s.text)
            buf.append("")
            _walk(s.children, buf)

    md_buf: list[str] = [f"# {title}", ""]
    _walk(sections, md_buf)
    markdown = "\n".join(md_buf).strip()

    return CleanDocument(title=title, markdown=markdown, sections=sections)
