#!/usr/bin/env python3
"""Scan configured public ATS boards and emit normalized, deduplicated leads."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from settings import config_data, term_matches
from typing import Any
from urllib.request import Request, urlopen

from factorial_public import collect_board as collect_factorial_board
from pipeline import annotate_job, config, keeps_low_fit_role, record_jobs, relevant_role_title, write_discovery_report

ROOT = Path(__file__).resolve().parents[1]
BOARDS_PATH = ROOT / "config" / "ats-boards.json"
USER_AGENT = "job-search-starter/2.0"


def fetch_data(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def joined_location(*values: Any) -> str:
    locations: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            locations.append(value.strip())
        elif isinstance(value, dict):
            name = value.get("name") or value.get("location")
            if name:
                locations.append(str(name).strip())
        elif isinstance(value, list):
            locations.extend(joined_location(item) for item in value)
    return "; ".join(dict.fromkeys(item for item in locations if item))


def normalize_ashby(board: dict[str, Any], raw: dict[str, Any], search_config: dict[str, Any]) -> dict[str, Any]:
    location = joined_location(raw.get("location"), raw.get("secondaryLocations"))
    job = {
        "source_identity": f"ashby:{board['slug']}:{raw.get('id')}",
        "source_job_id": str(raw.get("id") or ""),
        "source_portal": "ashby",
        "company": board["company"],
        "title": raw.get("title"),
        "location": location,
        "posted_date": raw.get("publishedAt"),
        "url": raw.get("jobUrl") or raw.get("applyUrl"),
        "description": raw.get("descriptionPlain") or raw.get("descriptionHtml") or raw.get("description"),
        "work_mode": raw.get("workplaceType"),
        "countries": [],
        "regions": [],
        "skills": [],
        "employment_type": raw.get("employmentType"),
        "department": raw.get("department"),
        "team": raw.get("team"),
    }
    return annotate_job(job, search_config)


def normalize_greenhouse(board: dict[str, Any], raw: dict[str, Any], search_config: dict[str, Any]) -> dict[str, Any]:
    location = raw.get("location") or {}
    offices = raw.get("offices") or []
    job = {
        "source_identity": f"greenhouse:{board['slug']}:{raw.get('id')}",
        "source_job_id": str(raw.get("id") or ""),
        "source_portal": "greenhouse",
        "company": board["company"],
        "title": raw.get("title"),
        "location": joined_location(location) or joined_location(offices),
        "posted_date": raw.get("first_published"),
        "updated_at": raw.get("updated_at"),
        "url": raw.get("absolute_url"),
        "description": raw.get("content"),
        "work_mode": None,
        "countries": [],
        "regions": [],
        "skills": [],
        "requisition_id": raw.get("requisition_id"),
    }
    return annotate_job(job, search_config)


def lever_date(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    return str(value) if value else None


def normalize_lever(board: dict[str, Any], raw: dict[str, Any], search_config: dict[str, Any]) -> dict[str, Any]:
    categories = raw.get("categories") or {}
    country = raw.get("country")
    job = {
        "source_identity": f"lever:{board['slug']}:{raw.get('id')}",
        "source_job_id": str(raw.get("id") or ""),
        "source_portal": "lever",
        "company": board["company"],
        "title": raw.get("text"),
        "location": joined_location(categories.get("location"), categories.get("allLocations")),
        "posted_date": lever_date(raw.get("createdAt")),
        "url": raw.get("hostedUrl") or raw.get("applyUrl"),
        "description": raw.get("descriptionPlain") or raw.get("description"),
        "work_mode": raw.get("workplaceType"),
        "countries": [country] if country else [],
        "regions": [],
        "skills": [],
        "employment_type": categories.get("commitment"),
        "department": categories.get("department"),
        "team": categories.get("team"),
    }
    return annotate_job(job, search_config)


def normalize_factorial(board: dict[str, Any], raw: dict[str, Any], search_config: dict[str, Any]) -> dict[str, Any]:
    job = {
        "source_identity": f"factorial:{board['slug']}:{raw['id']}",
        "source_job_id": raw["id"],
        "source_portal": "factorial",
        "company": board["company"],
        "title": raw["title"],
        "location": raw["location"],
        "posted_date": None,
        "url": raw["url"],
        "description": raw["description"],
        "work_mode": raw["work_mode"],
        "countries": [],
        "regions": [],
        "skills": [],
    }
    return annotate_job(job, search_config)


def endpoint(board: dict[str, Any]) -> str:
    kind = board["kind"]
    slug = board["slug"]
    pattern = r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*" if kind == "ashby" else r"[A-Za-z0-9_-]+"
    if not re.fullmatch(pattern, slug):
        raise ValueError("Invalid public board slug")
    if kind == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    if kind in {"greenhouse", "greenhouse_eu"}:
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    if kind == "lever":
        host = "api.eu.lever.co" if board.get("instance") == "eu" else "api.lever.co"
        return f"https://{host}/v0/postings/{slug}?mode=json"
    if kind == "workable":
        return f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    if kind == "bamboohr":
        return f"https://{slug}.bamboohr.com/careers/list"
    raise ValueError(f"Unsupported ATS kind: {kind}")


def normalize_workable(board: dict, raw: dict, search_config: dict) -> dict:
    shortcode = str(raw.get("shortcode") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", shortcode):
        raise ValueError("Workable job missing a valid shortcode")
    return annotate_job({
        "source_identity": f"workable:{board['slug']}:{shortcode}", "source_job_id": shortcode,
        "source_portal": "workable", "company": board["company"], "title": raw.get("title"),
        "url": f"https://apply.workable.com/{board['slug']}/j/{shortcode}/",
        "description": raw.get("description"), "posted_date": raw.get("published_on"),
        "created_at": raw.get("created_at"),
        "location": joined_location(raw.get("city"), raw.get("state"), raw.get("country")),
        "work_mode": "remote" if raw.get("telecommuting") is True else None,
        "employment_type": raw.get("employment_type"), "department": raw.get("department"),
    }, search_config)


def normalize_bamboohr(board: dict, raw: dict, search_config: dict) -> dict:
    job_id = str(raw.get("id") or "")
    if not job_id.isdigit():
        raise ValueError("BambooHR job missing a numeric ID")
    location = raw.get("location") or {}
    return annotate_job({
        "source_identity": f"bamboohr:{board['slug']}:{job_id}", "source_job_id": job_id,
        "source_portal": "bamboohr", "company": board["company"], "title": raw.get("jobOpeningName"),
        "url": f"https://{board['slug']}.bamboohr.com/careers/{job_id}",
        "description": raw.get("description"), "posted_date": raw.get("datePosted"),
        "location": joined_location(location.get("city"), location.get("state"), location.get("addressCountry")),
        "work_mode": "remote" if raw.get("isRemote") is True else None,
        "employment_type": raw.get("employmentStatusLabel"), "department": raw.get("departmentLabel"),
        "active_status": "listed_open", "source_location_type": raw.get("locationType"),
    }, search_config)


def fetch_bamboohr(board: dict) -> list[dict]:
    payload = fetch_data(endpoint(board))
    rows = payload.get("result")
    if not isinstance(rows, list) or payload.get("meta", {}).get("totalCount") != len(rows):
        raise ValueError("BambooHR board incomplete or unreadable; totalCount did not match")
    if len(rows) > 1000:
        raise ValueError("BambooHR board exceeds bounded detail limit; not fully scanned")
    result = []
    for row in rows:
        job_id = str(row.get("id") or "")
        if not job_id.isdigit():
            raise ValueError("Invalid BambooHR job ID")
        detail = fetch_data(f"https://{board['slug']}.bamboohr.com/careers/{job_id}/detail")
        job = detail.get("result", {}).get("jobOpening")
        if not isinstance(job, dict) or not job.get("jobOpeningStatus"):
            raise ValueError("BambooHR detail is unreadable; cannot establish board completion")
        if job["jobOpeningStatus"].lower() == "open":
            result.append({**job, "id": job_id})
    return result


def normalize_board_payload(
    board: dict[str, Any], payload: Any, search_config: dict[str, Any]
) -> list[dict[str, Any]]:
    kind = board["kind"]
    if kind == "ashby":
        rows = payload.get("jobs", [])
        return [normalize_ashby(board, row, search_config) for row in rows if row.get("isListed", True)]
    if kind in {"greenhouse", "greenhouse_eu"}:
        return [normalize_greenhouse(board, row, search_config) for row in payload.get("jobs", [])]
    if kind == "lever":
        return [normalize_lever(board, row, search_config) for row in payload]
    if kind == "factorial":
        return [normalize_factorial(board, row, search_config) for row in payload]
    if kind == "workable":
        if not isinstance(payload.get("jobs"), list):
            raise ValueError("Workable widget unreadable; jobs array missing")
        return [normalize_workable(board, row, search_config) for row in payload["jobs"]]
    if kind == "bamboohr":
        return [normalize_bamboohr(board, row, search_config) for row in payload]
    raise ValueError(f"Unsupported ATS kind: {kind}")


def select_boards(boards: list[dict[str, Any]], lane: str, names: list[str]) -> list[dict[str, Any]]:
    selected = [board for board in boards if board.get("enabled", True)]
    if lane not in {"all", "global"}:
        selected = [board for board in selected if lane in board.get("lanes", [])]
    if names:
        wanted = {name.lower() for name in names}
        selected = [
            board
            for board in selected
            if board["company"].lower() in wanted or board["slug"].lower() in wanted
        ]
    return selected


def lane_matches(job: dict[str, Any], lane: str, board: dict[str, Any]) -> bool:
    if lane == "all":
        return True
    details = config()["lanes"].get(lane, {})
    if details.get("remote_only"):
        haystack = f"{job.get('location', '')} {job.get('description', '')}".lower()
        return job.get("work_mode") == "remote" or "remote" in haystack or job["mobility_signal"] == "worldwide_claim"
    haystack = f" {job.get('title', '')} {job.get('location', '')} ".lower()
    locations = details.get("locations", [])
    if not locations or any(term_matches(term, haystack) for term in locations):
        return True
    return lane in board.get("allow_ambiguous_remote_lanes", []) and (
        job.get("work_mode") == "remote" or "remote" in str(job.get("location", "")).lower()
    )


def board_fit_matches(job: dict[str, Any], board: dict[str, Any]) -> bool:
    # Language-flexible boards and sponsorship-stating roles can omit stack keywords; preserve them for review.
    return (
        job["fit_signal"] in {"high", "medium"}
        or board.get("include_low_fit_roles") is True
        or keeps_low_fit_role(job)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", default="all", help="Agent lane from config, or all")
    parser.add_argument("--board", action="append", default=[], help="Company or board slug; repeatable")
    parser.add_argument("--no-state", action="store_true", help="Do not update cross-run deduplication state")
    args = parser.parse_args()

    board_config = config_data(BOARDS_PATH.name)
    search_config = config_data("search-config.json", require_profile=True)
    if args.lane != "all" and args.lane not in search_config["lanes"]:
        print(f"Unknown lane {args.lane!r}", file=sys.stderr)
        return 2
    boards = select_boards(board_config["boards"], args.lane, args.board)
    if not boards:
        print("No ATS boards matched the selection", file=sys.stderr)
        return 2

    jobs: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    source_checks: list[dict[str, Any]] = []
    for board in boards:
        label = f"{board['kind']}:{board['slug']}"
        try:
            if board["kind"] == "factorial":
                payload = collect_factorial_board(board["slug"])
            elif board["kind"] == "bamboohr":
                payload = fetch_bamboohr(board)
            else:
                payload = fetch_data(endpoint(board))
            rows = normalize_board_payload(board, payload, search_config)
            selected = [
                job
                for job in rows
                if board_fit_matches(job, board)
                and relevant_role_title(job["title"], search_config)
                and lane_matches(job, args.lane, board)
            ]
            for job in selected:
                if job["fit_signal"] == "low":
                    job["manual_fit_review"] = True
            jobs.extend(selected)
            source_checks.append({"source": label, "status": "completed", "roles_normalized": len(rows), "matching_jobs": len(selected)})
        except Exception as exc:
            failures.append({"source": label, "error": str(exc)})
            source_checks.append({"source": label, "status": "failed", "error": str(exc)})

    counts = record_jobs(jobs, "ats_direct", persist=not args.no_state)
    report, raw = write_discovery_report("ats", args.lane, jobs, failures, source_checks=source_checks)
    print(f"Boards: {len(boards)} | Completed: {len(boards) - len(failures)} | Matching jobs: {len(jobs)} | New: {counts['new']} | Failures: {len(failures)}")
    print(f"Report: {report}")
    print(f"Raw data: {raw}")
    return 1 if failures and not jobs else 0


if __name__ == "__main__":
    raise SystemExit(main())
