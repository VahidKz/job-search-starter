"""Profile-driven discovery for an agent. Only read public job board feeds."""

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


FEEDS = {
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json",
    "lever-eu": "https://api.eu.lever.co/v0/postings/{}?mode=json",
}
ATS_SITES = ("jobs.ashbyhq.com", "job-boards.greenhouse.io", "jobs.lever.co")
SLUG = re.compile(r"[a-zA-Z0-9_-][a-zA-Z0-9._-]{0,99}")


def phrases(profile, key, required=False):
    values = profile.get(key, [])
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        raise ValueError(f"{key} must be an array of non-empty strings")
    if required and not values:
        raise ValueError(f"{key} must contain at least one value")
    return values


def load_profile(path):
    profile = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(profile, dict) or profile.get("confirmed") is not True:
        raise ValueError("Confirm the search profile in chat before running discovery")
    for key in ("target_titles", "search_terms", "search_locations"):
        phrases(profile, key, required=key == "target_titles")
    boards = profile.get("boards", [])
    if not isinstance(boards, list) or any(
        not isinstance(board, dict)
        or not isinstance(board.get("platform"), str)
        or board["platform"] not in FEEDS
        or not isinstance(board.get("slug"), str)
        or not SLUG.fullmatch(board["slug"])
        for board in boards
    ):
        raise ValueError("boards must contain supported platforms and valid board slugs")
    return profile


def quote(value):
    return '"' + value.replace('"', "").strip() + '"'


def plan(profile):
    locations = phrases(profile, "search_locations") or [""]
    title_queries = [
        " ".join(part for part in (prefix, quote(title), quote(location) if location else "") if part)
        for title in phrases(profile, "target_titles", required=True)
        for location in locations
        for prefix in ("", *(f"site:{site}" for site in ATS_SITES))
    ]
    skill_queries = [
        " ".join(part for part in (quote(term), "jobs", quote(location) if location else "") if part)
        for term in phrases(profile, "search_terms")
        for location in locations
    ]
    return list(dict.fromkeys(title_queries + skill_queries))


def is_job_url(value):
    if not isinstance(value, str) or not value.startswith("https://"):
        return False
    try:
        parsed = urlsplit(value)
        return bool(parsed.hostname) and parsed.username is None
    except ValueError:
        return False


def read_feed(platform, slug):
    request = Request(FEEDS[platform].format(slug), headers={
        "Accept": "application/json",
        "User-Agent": "job-search-starter/1.0",
    })
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def timestamp(platform, row):
    raw = row.get({"ashby": "publishedAt", "greenhouse": "updated_at"}.get(platform, "createdAt"))
    try:
        if platform.startswith("lever"):
            return datetime.fromtimestamp(raw / 1000, timezone.utc)
        date = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return date.astimezone(timezone.utc) if date.tzinfo else None
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


def scan(profile, days, fetch=read_feed, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    terms = phrases(profile, "target_titles", required=True) + phrases(profile, "search_terms")
    matchers = [re.compile(r"(?<!\w)" + re.escape(term.strip()) + r"(?!\w)", re.I) for term in terms]
    leads, coverage, seen = [], [], set()
    for board in profile.get("boards", []):
        platform, slug = board["platform"], board["slug"]
        receipt = {"platform": platform, "board": slug}
        try:
            payload = fetch(platform, slug)
            rows = payload if platform.startswith("lever") else payload.get("jobs")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("Unexpected job feed format")
        except (URLError, TimeoutError, OSError, AttributeError, ValueError) as exc:
            coverage.append({**receipt, "status": "unavailable", "reason": type(exc).__name__})
            continue
        coverage.append({**receipt, "status": "read", "rows": len(rows)})
        for row in rows:
            title = row.get("text") if platform.startswith("lever") else row.get("title")
            url = row.get({"ashby": "jobUrl", "greenhouse": "absolute_url"}.get(platform, "hostedUrl"))
            if not isinstance(title, str) or not any(pattern.search(title) for pattern in matchers):
                continue
            if not is_job_url(url):
                continue
            date = timestamp(platform, row)
            if date and date < cutoff:
                continue
            requisition_id = row.get("id")
            if not isinstance(requisition_id, (str, int)):
                requisition_id = None
            identity = (platform, slug, requisition_id or url.split("?", 1)[0].rstrip("/"))
            if identity in seen:
                continue
            seen.add(identity)
            categories = row.get("categories")
            location = row.get("location") or (categories.get("location") if isinstance(categories, dict) else None)
            if isinstance(location, dict):
                location = location.get("name")
            leads.append({
                **receipt, "title": title, "url": url, "requisition_id": requisition_id,
                "location_text": location, "source_date": date.isoformat() if date else None,
                "date_kind": ("updated" if platform == "greenhouse" else
                              "published" if platform == "ashby" else "created") if date else "unknown",
                "status": "unverified",
            })
    return {"coverage": coverage, "status": (
        "not_configured" if not coverage else
        "partial_or_unavailable" if any(row["status"] != "read" for row in coverage) else "read"
    ), "window_days": days, "leads": leads}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "scan"))
    parser.add_argument("--profile", type=Path, default=Path(".local/profile.json"))
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.days <= 3650:
        parser.error("--days must be between 1 and 3650")
    try:
        profile = load_profile(args.profile)
        result = {"queries": plan(profile)} if args.command == "plan" else scan(profile, args.days)
        data = json.dumps(result, ensure_ascii=True, indent=2) + "\n"
        if args.output:
            args.output.write_text(data, encoding="utf-8")
        else:
            print(data, end="")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"job search: {exc}\n")


if __name__ == "__main__":
    main()
