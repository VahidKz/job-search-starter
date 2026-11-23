#!/usr/bin/env python3
"""Probe one job URL for reachability, redirects, route type, and closure text."""

from __future__ import annotations

import argparse
import json
import re
from html import unescape
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from application_checks import check_posting

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config" / "search-config.json").read_text(encoding="utf-8"))
USER_AGENT = "Mozilla/5.0 (compatible; JobSearchVerifier/1.0; personal-use)"
CLOSED_PATTERNS = [
    r"no longer accepting applications",
    r"position (?:has been|is) filled",
    r"job (?:is )?no longer available",
    r"vacancy (?:has been|is) closed",
    r"position_not_found",
    r"job not found",
]


def matches_host(host: str, names: list[str]) -> bool:
    return any(host == name or host.endswith("." + name) for name in names)


def classify(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":", 1)[0]
    if matches_host(host, CONFIG["official_ats_hosts"]):
        return "official_ats_candidate"
    if matches_host(host, CONFIG["aggregator_hosts"]):
        return "aggregator"
    return "employer_or_unknown"


def inspect(url: str) -> dict[str, object]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    with urlopen(request, timeout=30) as response:
        body = response.read(1_500_000).decode(response.headers.get_content_charset() or "utf-8", errors="replace")
        final_url = response.geturl()
        status = response.status
    guarded = check_posting(body, "html")
    text = guarded["text"]
    closed_matches = [pattern for pattern in CLOSED_PATTERNS if re.search(pattern, text, flags=re.I)]
    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.I | re.S)
    return {
        "requested_url": url,
        "final_url": final_url,
        "http_status": status,
        "redirected": final_url.rstrip("/") != url.rstrip("/"),
        "route_kind": classify(final_url),
        "page_title": unescape(re.sub(r"\s+", " ", title_match.group(1))).strip() if title_match else None,
        "closed_signal": bool(closed_matches),
        "closed_patterns": closed_matches,
        "posting_integrity": guarded["integrity"],
        "note": "A reachable page without closure text is still not proof that the vacancy accepts applications or supports the candidate's location.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    args = parser.parse_args()
    try:
        print(json.dumps(inspect(args.url), indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:  # A CLI probe should report network/parser failures as JSON.
        print(json.dumps({"requested_url": args.url, "error": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
