"""Read public, server-rendered Factorial career pages without private APIs."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlparse
from urllib.request import Request, urlopen

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class BoardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.jobs: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.depth = 0
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "li" and "job-offer-item" in (attributes.get("class") or "").split():
            self.current = {
                "url": attributes.get("data-job-postings-url") or "",
                "work_mode": "remote" if attributes.get("data-is-remote") == "true" else "onsite_or_hybrid",
            }
            self.depth = 1
            self.title_parts = []
        elif self.current is not None and tag not in VOID_TAGS:
            self.depth += 1

    def handle_data(self, data: str) -> None:
        if self.current is not None and len(self.title_parts) == 0 and data.strip():
            self.title_parts.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        self.depth -= 1
        if self.depth == 0:
            self.current["title"] = " ".join(self.title_parts)
            self.jobs.append(self.current)
            self.current = None


class JobParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.description_parts: list[str] = []
        self.in_h1 = False
        self.description_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "h1":
            self.in_h1 = True
        if tag == "div" and "styledText" in (attributes.get("class") or "").split():
            self.description_depth = 1
        elif self.description_depth and tag not in VOID_TAGS:
            self.description_depth += 1

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if self.in_h1 and value:
            self.title_parts.append(value)
        if self.description_depth and value:
            self.description_parts.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1":
            self.in_h1 = False
        if self.description_depth:
            self.description_depth -= 1


def valid_job_url(url: str, slug: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == f"{slug}.factorial.com"
        and bool(re.fullmatch(r"/job_posting/[a-z0-9-]+-\d+/?", parsed.path))
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def fetch_html(url: str) -> str:
    request = Request(url, headers={"Accept": "text/html", "User-Agent": "job-search-starter/2.0"})
    with urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8")


def collect_board(slug: str) -> list[dict[str, str]]:
    from pipeline import relevant_role_title

    parser = BoardParser()
    parser.feed(fetch_html(f"https://{slug}.factorial.com/"))
    if not parser.jobs:
        raise ValueError("Factorial board returned no job items; check page format or board status")
    jobs: list[dict[str, str]] = []
    for row in parser.jobs:
        if not relevant_role_title(row["title"]):
            continue
        if not valid_job_url(row["url"], slug):
            raise ValueError(f"Unexpected Factorial job URL: {row['url']}")
        page = JobParser()
        page.feed(fetch_html(row["url"]))
        if not page.title_parts or not page.description_parts:
            raise ValueError(f"Factorial job page missing title or description: {row['url']}")
        jobs.append({
            **row,
            "title": " ".join(page.title_parts),
            "description": " ".join(page.description_parts),
            "id": row["url"].rstrip("/").rsplit("-", 1)[-1],
            "location": "Remote (country scope unverified)" if row["work_mode"] == "remote" else "Location unverified",
        })
    return jobs
