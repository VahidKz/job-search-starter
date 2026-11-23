#!/usr/bin/env python3
"""Cross-check an employer against official sponsor registers where automation is reliable."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen

USER_AGENT = "job-search-starter/2.0"
SOURCES = {
    "uk": "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers",
    "nl": "https://ind.nl/en/public-register-recognised-sponsors/public-register-work",
    "nz": "https://www.immigration.govt.nz/work/requirements-for-work-visas/approved-employers/accredited-employer-list/",
}
LEGAL_SUFFIXES = {"limited", "ltd", "plc", "llp", "inc", "incorporated", "corp", "corporation", "bv", "b", "v"}


def fetch(url: str) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/csv,*/*"})
    with urlopen(request, timeout=60) as response:
        return response.read(), response.geturl()


def normalize_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    tokens = re.findall(r"[a-z0-9]+", ascii_text)
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def candidate_matches(company: str, records: list[dict[str, str]]) -> list[dict[str, Any]]:
    query = normalize_name(company)
    matches: list[dict[str, Any]] = []
    for record in records:
        name = record.get("name", "")
        normalized = normalize_name(name)
        ratio = SequenceMatcher(None, query, normalized).ratio() if normalized else 0.0
        if query == normalized:
            confidence = "exact_normalized_name"
        elif (
            min(len(query), len(normalized)) >= 6
            and (query in normalized or normalized in query)
            and bool(set(query.split()) & set(normalized.split()))
        ):
            confidence = "possible_legal_entity"
        elif min(len(query), len(normalized)) >= 6 and ratio >= 0.84:
            confidence = "possible_fuzzy_match"
        else:
            continue
        matches.append({**record, "confidence": confidence, "similarity": round(ratio, 3)})
    exact = [item for item in matches if item["confidence"] == "exact_normalized_name"]
    if exact:
        return exact[:12]
    return sorted(matches, key=lambda item: item["similarity"], reverse=True)[:12]


def uk_records() -> list[dict[str, str]]:
    page_bytes, page_url = fetch(SOURCES["uk"])
    page = page_bytes.decode("utf-8", errors="replace")
    links = re.findall(r'href=["\']([^"\']+\.csv(?:\?[^"\']*)?)["\']', page, flags=re.I)
    if not links:
        raise RuntimeError("The current GOV.UK page did not expose a CSV link")
    csv_bytes, csv_url = fetch(urljoin(page_url, unescape(links[0])))
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    rows = csv.DictReader(io.StringIO(text))
    records = []
    for row in rows:
        records.append(
            {
                "name": row.get("Organisation Name", ""),
                "city": row.get("Town/City", ""),
                "rating": row.get("Type & Rating", ""),
                "route": row.get("Route", ""),
                "source_url": csv_url,
            }
        )
    return records


class TableRows(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_cell = False
        self.cell_parts: list[str] = []
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"td", "th"}:
            self.in_cell = True
            self.cell_parts = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.in_cell:
            self.row.append(re.sub(r"\s+", " ", " ".join(self.cell_parts)).strip())
            self.in_cell = False
        elif tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = []


def nl_records() -> list[dict[str, str]]:
    page_bytes, final_url = fetch(SOURCES["nl"])
    parser = TableRows()
    parser.feed(page_bytes.decode("utf-8", errors="replace"))
    records = []
    for row in parser.rows:
        if len(row) >= 2 and row[0].lower() not in {"organisation", "organization"}:
            records.append({"name": row[0], "kvk": row[1], "source_url": final_url})
    if not records:
        raise RuntimeError("The current IND page did not expose parseable register rows")
    return records


def verify(country: str, company: str) -> dict[str, Any]:
    if country == "nz":
        return {
            "country": "nz",
            "company": company,
            "status": "manual_official_lookup_required",
            "matches": [],
            "source_url": SOURCES["nz"],
            "note": "Immigration New Zealand uses an interactive daily-updated lookup. Search the exact legal or trading name there.",
        }
    records = uk_records() if country == "uk" else nl_records()
    matches = candidate_matches(company, records)
    return {
        "country": country,
        "company": company,
        "status": "candidate_matches_found" if matches else "no_name_match_found",
        "matches": matches,
        "source_url": SOURCES[country],
        "note": "Register presence supports employer capability only. The exact vacancy must still confirm sponsorship.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", action="append", required=True, choices=sorted(SOURCES))
    parser.add_argument("--company", action="append", required=True)
    args = parser.parse_args()
    results = []
    failures = []
    for country in dict.fromkeys(args.country):
        for company in dict.fromkeys(args.company):
            try:
                results.append(verify(country, company))
            except Exception as exc:
                failures.append({"country": country, "company": company, "error": str(exc)})
    print(json.dumps({"results": results, "failures": failures}, indent=2, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
