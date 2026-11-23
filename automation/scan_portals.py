#!/usr/bin/env python3
"""Bounded public portal adapters. Jobbank RSS pattern adapted from pinned ai-job-search."""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from discover_jobs import clean_description
from pipeline import annotate_job, config, record_jobs, relevant_role_title, write_discovery_report

BASE = "https://jobbank.dk"
USER_AGENT = "JobSearchStarter/1.0 (private personal-use portal reader)"


def fetch_text(url: str) -> str:
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=20) as response:
        if urlparse(response.geturl()).hostname != "jobbank.dk":
            raise ValueError("Portal redirected off the allowed host; manual inspection required")
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Portal response exceeded size limit")
        return raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace")


def check_access() -> None:
    robot = RobotFileParser()
    try:
        text = fetch_text(BASE + "/robots.txt")
    except HTTPError as exc:
        if exc.code == 404:
            text = "User-agent: *\nAllow: /\n"
        else:
            raise ValueError("Robots/access check unavailable; use manual search, not a bypass: " + str(exc)) from exc
    robot.parse(text.splitlines())
    if not robot.can_fetch(USER_AGENT, BASE + "/job/rss"):
        raise ValueError("robots.txt disallows RSS; use manual search")


def parse_jobbank(xml: str, search_config: dict) -> list[dict]:
    # The feed is untrusted XML; declarations/entities are unnecessary for RSS here.
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", xml, re.I):
        raise ValueError("XML declarations/entities rejected")
    document = ET.fromstring(xml)
    if document.tag.lower() != "rss" or document.find("channel") is None:
        raise ValueError("Expected RSS, not a challenge/login HTML page")
    rows = []
    for item in document.findall("./channel/item"):
        title, url = item.findtext("title"), item.findtext("link")
        if not title or not url or urlparse(url).scheme not in {"https", "http"}:
            continue
        description = item.findtext("description") or ""
        visible = clean_description(description)
        # Upstream feed format: job type hos Company, Location (application deadline).
        match = re.search(r"\shos\s+(.+?),\s+(.+?)(?:\s*\(|$)", visible)
        if not match:
            continue
        published = None
        try:
            day = parsedate_to_datetime(item.findtext("pubDate") or "")
            if not day.tzinfo:
                day = day.replace(tzinfo=timezone.utc)
            published = day.isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
        deadline = None
        deadline_match = re.search(r"(?:Ans\u00f8gningsfrist|Ansogningsfrist):\s*(\d{2}\.\d{2}\.\d{4})", visible, re.I)
        if deadline_match:
            try:
                deadline = datetime.strptime(deadline_match[1], "%d.%m.%Y").date().isoformat()
            except ValueError:
                pass
        rows.append(annotate_job({"company": match[1], "title": title, "location": match[2],
                                  "url": url.strip(), "source_identity": "jobbank:" + url.strip(),
                                  "source_portal": "jobbank_rss", "description": description,
                                  "posted_date": published, "deadline": deadline, "countries": ["dk"],
                                  "regions": ["Denmark"], "work_mode": None}, search_config))
    return rows


def collect(queries: list[str], *, days: int = 7, limit: int = 20) -> tuple[list[dict], list[dict], list[dict]]:
    if days < 1 or limit < 1 or not queries or any(not q.strip() for q in queries):
        raise ValueError("Nonempty queries, positive days and positive limit required")
    receipts, failures, found = [], [], {}
    try:
        check_access()
    except Exception as exc:
        return [], [{"source": "jobbank", "error": str(exc)}], [
            {"source": "jobbank", "status": "blocked_access_check", "queries_executed": 0,
             "note": "No RSS request made. Manual discovery remains pending."}]
    cfg = config()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for query in queries:
        url = BASE + "/job/rss?" + urlencode({"key": query, "oprettet": cutoff.date().isoformat()})
        try:
            rows = parse_jobbank(fetch_text(url), cfg)
            for row in rows:
                posted = datetime.fromisoformat(row["posted_date"]) if row.get("posted_date") else None
                if (posted is None or posted >= cutoff) and relevant_role_title(row["title"], search_config):
                    found[row["url"]] = row
            receipts.append({"source": "jobbank", "query": query, "url": url, "status": "completed",
                             "parsed": len(rows), "scope": "bounded query RSS snapshot, not full portal"})
        except Exception as exc:
            failures.append({"source": "jobbank", "query": query, "error": str(exc)})
            receipts.append({"source": "jobbank", "query": query, "status": "failed"})
    return list(found.values())[:limit], failures, receipts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portal", choices=["jobbank"], default="jobbank")
    parser.add_argument("--query", action="append")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--no-state", action="store_true")
    args = parser.parse_args()
    try:
        jobs, failures, receipts = collect(args.query or config_data("search-config.json", require_profile=True)["target_titles"], days=args.days, limit=args.limit)
    except ValueError as exc:
        parser.error(str(exc))
    record_jobs(jobs, "public_portal", persist=not args.no_state)
    report, raw = write_discovery_report("portals", "global", jobs, failures, receipts)
    print(f"Report: {report}\nRaw data: {raw}")
    print(json.dumps({"leads": len(jobs), "failures": len(failures), "note": "Denmark discovery, not sponsorship proof."}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
