#!/usr/bin/env python3
"""Discover recent technical jobs through FreeHire and persist deduplicated leads.

This is deliberately a discovery feeder. It never claims that an aggregator result
is active, official, sponsor-backed, or eligible for a Turkey-based candidate.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from settings import config_data, term_matches
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from job_key import canonical_url, make_key
from job_history import reconcile_jobs, review_candidates
from application_checks import check_posting

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "search-config.json"
STATE_PATH = ROOT / ".local" / "state" / "seen_jobs.json"
RUNS_DIR = ROOT / ".local" / "state" / "runs"
HANDOFF_PATH = ROOT / ".local" / "handoff.md"
DEFAULT_API = "https://freehire.me"
USER_AGENT = "job-search-starter/2.0"
MOJIBAKE_MARKERS = ("\u00c3", "\u00c2", "\u00e2", "\u00f0")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def repair_text(value: Any) -> str:
    text = "" if value is None else str(value)
    if any(marker in text for marker in MOJIBAKE_MARKERS):
        for source_encoding in ("cp1252", "latin-1"):
            try:
                repaired = text.encode(source_encoding).decode("utf-8")
                before = sum(text.count(marker) for marker in MOJIBAKE_MARKERS)
                after = sum(repaired.count(marker) for marker in MOJIBAKE_MARKERS)
                if after < before:
                    text = repaired
                    break
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
    return text.strip()


def clean_description(value: Any) -> str:
    text = check_posting(repair_text(value))["text"]
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:p|li|ul|ol|div|h\d)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def host_matches(host: str, candidates: list[str]) -> bool:
    host = host.lower().split(":", 1)[0]
    return any(host == candidate or host.endswith("." + candidate) for candidate in candidates)


def route_kind(url: str, config: dict[str, Any]) -> str:
    host = urlparse(url).netloc.lower()
    if host_matches(host, config["official_ats_hosts"]):
        return "official_ats_candidate"
    if host_matches(host, config["aggregator_hosts"]):
        return "aggregator"
    return "employer_or_unknown"


def mobility_signal(job: dict[str, Any]) -> str:
    haystack = " ".join(
        [
            job.get("title", ""),
            job.get("location", ""),
            job.get("description", ""),
            " ".join(job.get("countries", [])),
            " ".join(job.get("regions", [])),
        ]
    ).lower()
    haystack = repair_text(haystack).replace("\u2019", "'")
    sponsorship = re.search(
        r"\b(?:visa sponsorship|sponsor(?:ing|ship)? (?:a |the )?visa|work permit support|immigration support|relocation (?:support|assistance|package))\b",
        haystack,
    )
    worldwide = re.search(
        r"\b(?:work from anywhere|work anywhere|anywhere in the world|remote[- ]global|global remote|international remote|hire worldwide|hiring worldwide|worldwide remote)\b",
        haystack,
    )
    no_sponsorship = re.search(
        r"\b(?:no (?:visa |work permit )?sponsorship|(?:visa )?sponsorship (?:is |will be )?(?:not available|unavailable|not provided)|"
        r"(?:cannot|can't|unable to|not able to|do not|don't|does not|doesn't|will not|won't) (?:offer |provide |support )?(?:visa sponsorship|sponsor(?: visas?)?))\b",
        haystack,
    )
    rights_required = re.search(
        r"\b(?:must (?:already )?be authori[sz]ed|must (?:already )?have (?:the )?right to work|"
        r"right to work in|existing work authori[sz]ation|must hold (?:a |an )?(?:valid )?work permit)\b", haystack,
    )
    if rights_required:
        return "existing_work_rights_mentioned"
    if no_sponsorship:
        return "sponsorship_unavailable_mentioned"
    if sponsorship:
        return "sponsorship_or_relocation_mentioned"
    if worldwide:
        return "worldwide_claim"
    if job.get("work_mode") == "remote":
        return "remote_scope_unclear"
    return "onsite_or_scope_unclear"


def fit_signal(job: dict[str, Any], config: dict[str, Any]) -> tuple[str, list[str], int]:
    title = job.get("title", "").lower()
    description = job.get("description", "").lower()
    skills = " ".join(job.get("skills", [])).lower()
    haystack = " ".join((title, skills, description))
    groups = config["fit_terms"]
    matches: list[str] = []
    score = 0
    weights = {"primary": 4, "secondary": 3, "domain": 2, "platform": 1}
    for group, terms in groups.items():
        found = sorted({term for term in terms if term_matches(term, haystack)})
        matches.extend(found)
        score += min(len(found), 4) * weights[group]
    for term in groups["primary"] + groups["secondary"]:
        if term_matches(term, title):
            score += 3
    if any(term_matches(term, title) for term in config.get("title_terms", [])):
        score += 10
    level = "high" if score >= 18 else "medium" if score >= 9 else "low"
    return level, matches[:12], score


def normalize_job(raw: dict[str, Any], query_name: str, config: dict[str, Any]) -> dict[str, Any]:
    url = canonical_url(repair_text(raw.get("url")))
    guarded = check_posting(repair_text(raw.get("description")))
    job = {
        "id": repair_text(raw.get("public_slug")),
        "title": repair_text(raw.get("title")) or "(untitled)",
        "company": repair_text(raw.get("company")) or "Unknown company",
        "location": repair_text(raw.get("location")),
        "posted_date": repair_text(raw.get("posted_at")) or None,
        "url": url,
        "source_portal": repair_text(raw.get("source")) or "freehire",
        "work_mode": repair_text(raw.get("work_mode")) or None,
        "countries": [repair_text(item).lower() for item in raw.get("countries", [])],
        "regions": [repair_text(item).lower() for item in raw.get("regions", [])],
        "skills": [repair_text(item).lower() for item in raw.get("skills", [])],
        "description": clean_description(guarded["text"]),
        "posting_integrity": guarded["integrity"],
        "query": query_name,
    }
    fit, matched_terms, sort_score = fit_signal(job, config)
    job["fit_signal"] = fit
    job["matched_terms"] = matched_terms
    job["sort_score"] = sort_score
    job["mobility_signal"] = mobility_signal(job)
    job["route_kind"] = route_kind(url, config)
    job["key"] = make_key(job["company"], job["title"], url)
    return job


def fetch_json(url: str, retries: int = 4) -> dict[str, Any]:
    delay = 0.5
    for attempt in range(retries + 1):
        request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise
            if attempt == retries:
                raise
        except URLError:
            if attempt == retries:
                raise
        time.sleep(delay)
        delay = min(delay * 2, 8)
    raise RuntimeError("request failed after retries")


def search_freehire(query: str, days: int, limit: int, countries: list[str], remote: bool) -> list[dict[str, Any]]:
    params: list[tuple[str, str]] = [
        ("q", query),
        ("limit", str(limit)),
        ("offset", "0"),
        ("posted_within_days", str(days)),
        ("include_description", "true"),
        ("description_format", "text"),
    ]
    params.extend(("countries", country.lower()) for country in countries)
    if remote:
        params.append(("work_mode", "remote"))
    base = os.environ.get("FREEHIRE_API_URL", DEFAULT_API).rstrip("/")
    payload = fetch_json(f"{base}/api/v1/agent/jobs/search?{urlencode(params)}")
    data = payload.get("data", [])
    if not isinstance(data, list):
        raise RuntimeError("FreeHire returned an unexpected data shape")
    return data


def known_in_handoff(job: dict[str, Any], handoff_text: str) -> bool:
    url = job["url"].lower()
    if url and url in handoff_text:
        return True
    company = re.sub(r"\W+", " ", job["company"].lower()).strip()
    title = re.sub(r"\W+", " ", job["title"].lower()).strip()
    for line in handoff_text.splitlines():
        normalized = re.sub(r"\W+", " ", line.lower()).strip()
        if company and title and company in normalized and title in normalized:
            return True
    return False


def markdown_escape(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def write_report(path: Path, metadata: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
    candidates = review_candidates(jobs)
    mobility_rank = {
        "sponsorship_or_relocation_mentioned": 4,
        "worldwide_claim": 3,
        "remote_scope_unclear": 2,
        "onsite_or_scope_unclear": 1,
    }
    candidates.sort(key=lambda job: (mobility_rank.get(job["mobility_signal"], 0), job["sort_score"]), reverse=True)
    lines = [
        f"# Discovery Run: {metadata['created_at']}",
        "",
        "> Discovery only. Verify the original, mobility, freshness and local history. Inbox not accessed.",
        "",
        f"Lane: `{metadata['lane']}` | Window: {metadata['days']} days | Queries: {len(metadata['queries'])} | Unique results: {metadata['unique_results']} | Review candidates: {len(candidates)} (including unreviewed backlog)",
        "Showing up to 40; full results and persistent review queue retain the remainder. Technical fit is not eligibility.",
        "",
    ]
    if candidates:
        lines.extend(
            [
                "| Fit | Mobility text signal | Route | Company | Exact title | Posted | Link |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for job in candidates[:40]:
            lines.append(
                "| {fit} | `{mobility}` | `{route}` | {company} | {title} | {posted} | [open]({url}) |".format(
                    fit=job["fit_signal"].title(),
                    mobility=markdown_escape(job["mobility_signal"]),
                    route=markdown_escape(job["route_kind"]),
                    company=markdown_escape(job["company"]),
                    title=markdown_escape(job["title"]),
                    posted=markdown_escape((job["posted_date"] or "")[:10]),
                    url=job["url"],
                )
            )
    else:
        lines.append("No unreviewed medium-or-better leads survived the automated prefilter.")
    lines.extend(
        [
            "",
            "## Next Gate",
            "",
            "For each promising row: confirm the original is active, verify candidate-location or sponsorship eligibility, inspect mandatory requirements and local application history. Inbox access is not part of discovery.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lane", default="global", help="Configured lane name (default: global)")
    parser.add_argument("--days", type=int, help="Posting-age window")
    parser.add_argument("--limit", type=int, help="Maximum results per query")
    parser.add_argument("--query", action="append", help="Override configured queries; repeatable")
    parser.add_argument("--country", action="append", help="Override lane country facet; repeatable ISO code")
    parser.add_argument("--remote", action="store_true", help="Request only FreeHire remote-tagged results")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = config_data(CONFIG_PATH.name, require_profile=not args.query)
    if args.lane not in config["lanes"]:
        print(f"Unknown lane {args.lane!r}. Available: {', '.join(config['lanes'])}", file=sys.stderr)
        return 2
    lane = config["lanes"][args.lane]
    days = args.days or config["default_days"]
    limit = args.limit or config["results_per_query"]
    queries = args.query or lane["queries"]
    if not queries:
        parser_message = "No search terms configured"
        print(parser_message, file=sys.stderr)
        return 2
    countries = args.country if args.country is not None else lane.get("countries", [])
    remote_only = args.remote or bool(lane.get("remote_only", False))
    if days < 1 or limit < 1:
        print("--days and --limit must be positive", file=sys.stderr)
        return 2

    collected: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, str]] = []
    for query in queries:
        try:
            for raw in search_freehire(query, days, limit, countries, remote_only):
                job = normalize_job(raw, query, config)
                identity = job["url"] or job["key"]
                if identity in collected:
                    previous = collected[identity]
                    previous["query"] = ", ".join(sorted(set(previous["query"].split(", ") + [query])))
                    if job["sort_score"] > previous["sort_score"]:
                        job["query"] = previous["query"]
                        collected[identity] = job
                else:
                    collected[identity] = job
        except (HTTPError, URLError, RuntimeError, json.JSONDecodeError) as exc:
            failures.append({"query": query, "error": str(exc)})

    state = load_json(STATE_PATH) if STATE_PATH.exists() else {"version": 1, "seen": {}}
    handoff_text = HANDOFF_PATH.read_text(encoding="utf-8").lower() if HANDOFF_PATH.exists() else ""
    now = datetime.now(timezone.utc)
    jobs = list(collected.values())
    counts = reconcile_jobs(state, jobs, "freehire_api", lambda job: known_in_handoff(job, handoff_text),
                            now.date().isoformat())
    new_count = counts["new"]
    save_json(STATE_PATH, state)

    stamp = now.strftime("%Y%m%d-%H%M%SZ")
    metadata = {
        "created_at": now.isoformat(),
        "lane": args.lane,
        "days": days,
        "queries": queries,
        "countries": countries,
        "remote_only": remote_only,
        "unique_results": len(jobs),
        "new_results": new_count,
        "failures": failures,
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RUNS_DIR / f"{stamp}-{args.lane}.json"
    report_path = RUNS_DIR / f"{stamp}-{args.lane}.md"
    save_json(raw_path, {"meta": metadata, "results": jobs})
    write_report(report_path, metadata, jobs)

    print(f"Unique results: {len(jobs)}")
    print(f"New unverified results: {new_count}")
    print(f"Report: {report_path}")
    print(f"Raw data: {raw_path}")
    if failures:
        print(f"Query failures: {len(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
