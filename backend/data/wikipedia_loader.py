"""Fetch a Wikipedia article via the MediaWiki API.

We avoid HTML scraping because the API gives us the parsed HTML *plus* the
section tree and the revision id in one stable JSON response. The User-Agent
follows the Wikimedia policy (see meta.wikimedia.org/wiki/User-Agent_policy).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from backend.config import get_settings


@dataclass
class WikipediaFetch:
    url: str
    title: str
    html: str
    revision_id: str
    etag: str | None
    content_hash: str
    sections: list[dict]  # [{toclevel, level, line, anchor, ...}, ...]


def _title_from_url(url: str) -> str:
    path = urlparse(url).path  # /wiki/Apple
    return unquote(path.rsplit("/", 1)[-1])


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def fetch_article(url: str | None = None) -> WikipediaFetch:
    settings = get_settings()
    url = url or settings.wikipedia_url
    title = _title_from_url(url)

    api_base = f"{urlparse(url).scheme}://{urlparse(url).netloc}/w/api.php"
    params = {
        "action": "parse",
        "page": title,
        "prop": "text|sections|revid",
        "format": "json",
        "redirects": 1,
        "disableeditsection": 1,
    }
    headers = {"User-Agent": settings.wikipedia_user_agent}

    with httpx.Client(timeout=30.0, headers=headers) as client:
        resp = client.get(api_base, params=params)
        resp.raise_for_status()
        data = resp.json()

    if "error" in data:
        raise RuntimeError(f"MediaWiki error: {data['error']}")

    parse = data["parse"]
    html = parse["text"]["*"]
    revid = str(parse.get("revid", ""))
    sections = parse.get("sections", [])
    content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    etag = resp.headers.get("etag")

    return WikipediaFetch(
        url=url,
        title=parse.get("title", title),
        html=html,
        revision_id=revid,
        etag=etag,
        content_hash=content_hash,
        sections=sections,
    )
