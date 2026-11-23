#!/usr/bin/env python3
"""Scan permitted public job feeds used by one or more agent lanes."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from settings import config_data
from typing import Any

from pipeline import annotate_job, config, keeps_low_fit_role, record_jobs, relevant_role_title, write_discovery_report
from scan_ats import fetch_data

ROOT = Path(__file__).resolve().parents[1]
LANES_PATH = ROOT / "config" / "agent-lanes.json"
FEEDS = {
    "remotive": "https://remotive.com/api/remote-jobs",
    "arbeitnow": "https://www.arbeitnow.com/api/job-board-api",
}


def normalize_remotive(raw: dict[str, Any], search_config: dict[str, Any]) -> dict[str, Any]:
    location = raw.get("candidate_required_location") or "Remote"
    return annotate_job(
        {
            "source_identity": f"remotive:{raw.get('id')}",
            "source_job_id": str(raw.get("id") or ""),
            "source_portal": "remotive",
            "company": raw.get("company_name"),
            "title": raw.get("title"),
            "location": location,
            "posted_date": raw.get("publication_date"),
            "url": raw.get("url"),
            "description": raw.get("description"),
            "work_mode": "remote",
            "countries": [],
            "regions": [location],
            "skills": [raw.get("category", "")],
            "employment_type": raw.get("job_type"),
            "salary": raw.get("salary"),
        },
        search_config,
    )


def normalize_arbeitnow(raw: dict[str, Any], search_config: dict[str, Any]) -> dict[str, Any]:
    created = raw.get("created_at")
    if isinstance(created, (int, float)):
        created = datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
    return annotate_job(
        {
            "source_identity": f"arbeitnow:{raw.get('slug')}",
            "source_job_id": str(raw.get("slug") or ""),
            "source_portal": "arbeitnow",
            "company": raw.get("company_name"),
            "title": raw.get("title"),
            "location": raw.get("location"),
            "posted_date": created,
            "url": raw.get("url"),
            "description": raw.get("description"),
            "work_mode": "remote" if raw.get("remote") else "onsite_or_hybrid",
            "countries": [],
            "regions": [],
            "skills": raw.get("tags") or [],
            "employment_type": ", ".join(raw.get("job_types") or []),
            "visa_sponsorship_field": raw.get("visa_sponsorship"),
        },
        search_config,
    )


def parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def recent(job: dict[str, Any], days: int) -> bool:
    posted = parse_date(job.get("posted_date"))
    if not posted:
        return True
    if not posted.tzinfo:
        posted = posted.replace(tzinfo=timezone.utc)
    return posted >= datetime.now(timezone.utc) - timedelta(days=days)


def feed_names(lane: str, requested: list[str]) -> list[str]:
    if requested:
        return list(dict.fromkeys(requested))
    lanes = config_data(LANES_PATH.name)["lanes"]
    if lane == "all":
        return sorted({name for item in lanes.values() for name in item.get("public_feeds", [])})
    if lane not in lanes:
        raise KeyError(lane)
    return lanes[lane].get("public_feeds", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", default="all")
    parser.add_argument("--source", action="append", default=[], choices=sorted(FEEDS))
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--no-state", action="store_true")
    args = parser.parse_args()
    if args.days < 1:
        print("--days must be positive", file=sys.stderr)
        return 2

    try:
        names = feed_names(args.lane, args.source)
    except KeyError:
        print(f"Unknown lane {args.lane!r}", file=sys.stderr)
        return 2
    if not names:
        print(f"Lane {args.lane!r} has no configured public feeds")
        return 0

    search_config = config_data("search-config.json", require_profile=True)
    jobs: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for name in names:
        try:
            payload = fetch_data(FEEDS[name])
            if name == "remotive":
                rows = [normalize_remotive(raw, search_config) for raw in payload.get("jobs", [])]
            else:
                rows = [normalize_arbeitnow(raw, search_config) for raw in payload.get("data", [])]
            selected = [
                job
                for job in rows
                if recent(job, args.days)
                and (job["fit_signal"] in {"high", "medium"} or keeps_low_fit_role(job))
                and relevant_role_title(job["title"], search_config)
            ]
            for job in selected:
                if job["fit_signal"] == "low":
                    job["manual_fit_review"] = True
            jobs.extend(selected)
        except Exception as exc:
            failures.append({"source": name, "error": str(exc)})

    counts = record_jobs(jobs, "public_feed", persist=not args.no_state)
    report, raw = write_discovery_report("feeds", args.lane, jobs, failures)
    print(f"Feeds: {len(names)} | Matching jobs: {len(jobs)} | New: {counts['new']} | Failures: {len(failures)}")
    print(f"Report: {report}")
    print(f"Raw data: {raw}")
    return 1 if failures and not jobs else 0


if __name__ == "__main__":
    raise SystemExit(main())
