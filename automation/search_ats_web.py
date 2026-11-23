#!/usr/bin/env python3
"""Search configured ATS hosts through the official Brave Search API.

Results are discovery leads, never verified vacancies or mobility evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from settings import config_data
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from discover_jobs import canonical_url, host_matches
from generate_ats_search_queries import CONFIG_PATH, ROOT, build_queries, build_query_plan, load

API_URL = "https://api.search.brave.com/res/v1/web/search"
RUNS = ROOT / ".local" / "state" / "runs"


def api_query(query: str) -> str:
    """The generated after: operator is Google-specific; Brave uses freshness."""
    return re.sub(r"\s+after:\d{4}-\d{2}-\d{2}$", "", query).strip()


def freshness(days: int, today: date) -> str:
    return f"{today - timedelta(days=days):%Y-%m-%d}to{today:%Y-%m-%d}"


def fetch_results(query: str, date_filter: str, api_key: str, count: int = 10) -> list[dict[str, Any]]:
    params = {"q": query, "count": count, "result_filter": "web"}
    if date_filter:
        params["freshness"] = date_filter
    params = urlencode(params)
    request = Request(
        f"{API_URL}?{params}",
        headers={"Accept": "application/json", "X-Subscription-Token": api_key},
    )
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    return payload.get("web", {}).get("results", [])


def target_hosts(config: dict[str, Any], target: str) -> list[str]:
    for group in config["platform_groups"]:
        if group["name"] == target:
            return [site for platform in group["platforms"] for site in platform["sites"]]
        for platform in group["platforms"]:
            if platform["name"] == target:
                return platform["sites"]
    raise ValueError(f"Unknown ATS target: {target}")


def collect(
    config: dict[str, Any],
    lane: str,
    days: int,
    tier: int,
    api_key: str,
    today: date | None = None,
    fetch: Callable[[str, str, str, int], list[dict[str, Any]]] = fetch_results,
    delay: float = 1.0,
    expanded: bool = False,
    mode: str = "lane",
    fallbacks: bool = False,
    query_cache: dict | None = None,
) -> dict[str, Any]:
    today = today or datetime.now().astimezone().date()
    builder = build_query_plan if fallbacks else build_queries
    queries = [item for item in builder(config, lane, days, today, expanded, mode) if item["tier"] <= tier]
    report: dict[str, Any] = {
        "lane": lane,
        "days": days,
        "tier": tier,
        "mode": mode,
        "provider": "brave_search_api",
        "status": "needs_credential" if not api_key else "completed",
        "queries_planned": len(queries),
        "queries_attempted": 0,
        "queries_succeeded": 0,
        "queries_skipped": 0,
        "queries_reused": 0,
        "query_receipts": [],
        "failures": [],
        "results": [],
        "note": "Search-index age is not the vacancy publication date; verify every employer page and country and visa eligibility.",
    }
    if not api_key:
        return report

    discovered: dict[str, dict[str, Any]] = {}
    resolved, failed = set(), set()
    date_filter = freshness(days, today)
    for item in queries:
        group = (item["target"], item["role_pack"])
        receipt = {"target": item["target"], "role_pack": item["role_pack"], "stage": item["stage"],
                   "query": item["query"], "status": "not_attempted", "accepted_results": 0}
        report["query_receipts"].append(receipt)
        if group in resolved or group in failed:
            receipt["status"] = "skipped_results_found" if group in resolved else "skipped_earlier_error"
            report["queries_skipped"] += 1
            continue
        query = api_query(item["query"])
        if len(query) > 600 or len(query.split()) > 75:
            report["failures"].append({"target": item["target"], "role_pack": item["role_pack"], "error": "Query exceeds Brave API limit"})
            receipt["status"] = "invalid_query"
            failed.add(group)
            continue
        try:
            effective_date = "" if item["stage"] == "stack_anytime" else date_filter
            cache_key = json.dumps([query, effective_date])
            cached = (query_cache or {}).get(cache_key, {})
            reused = cached.get("fetched_on") == today.isoformat() and isinstance(cached.get("rows"), list)
            if reused:
                rows = cached["rows"]
                report["queries_reused"] += 1
            else:
                report["queries_attempted"] += 1
                rows = fetch(query, effective_date, api_key, 10)
                report["queries_succeeded"] += 1
                if query_cache is not None:
                    query_cache[cache_key] = {"fetched_on": today.isoformat(), "rows": rows}
            receipt["reused_query_receipt"] = reused
            hosts = item["sites"]
            for row in rows:
                url = canonical_url(str(row.get("url") or ""))
                if not url.startswith("https://") or not host_matches(urlparse(url).hostname or "", hosts):
                    continue
                receipt["accepted_results"] += 1
                if url not in discovered:
                    discovered[url] = {
                        "url": url,
                        "title": row.get("title") or "",
                        "snippet": row.get("description") or "",
                        "search_page_age": row.get("age") or row.get("page_age"),
                        "source_target": item["target"],
                        "role_packs": [item["role_pack"]],
                        "status": "discovered_unverified",
                        "discovery_stage": item["stage"],
                        "date_scope": "anytime_not_recent_verified" if item["stage"] == "stack_anytime" else "recent_index_only",
                    }
                elif item["role_pack"] not in discovered[url]["role_packs"]:
                    discovered[url]["role_packs"].append(item["role_pack"])
            receipt["status"] = "results_found" if receipt["accepted_results"] else "empty"
            if reused:
                receipt["status"] = "cached_" + receipt["status"]
            if receipt["accepted_results"]:
                resolved.add(group)
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            code = getattr(exc, "code", None)
            receipt["status"] = "failed"
            failed.add(group)
            report["failures"].append({"target": item["target"], "role_pack": item["role_pack"], "error": f"HTTP {code}" if code else str(exc)})
            if code in {401, 403, 429}:
                report["status"] = "credential_rejected" if code in {401, 403} else "rate_limited"
                break
        if delay and not reused and report["queries_attempted"] < len(queries):
            time.sleep(delay)
    report["results"] = list(discovered.values())
    if report["status"] == "completed" and report["failures"]:
        report["status"] = "partial"
    return report


def write_report(output: Path, report: dict[str, Any]) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown = output.with_suffix(".md")
    lines = [
        f"# ATS Web Search: {report['lane']}",
        "",
        f"Status: `{report['status']}` | Queries: {report['queries_succeeded']}/{report['queries_planned']} succeeded | Leads: {len(report['results'])}",
        f"Reused same-day query receipts: {report.get('queries_reused', 0)}; skipped fallbacks: {report.get('queries_skipped', 0)}. Neither counts as a new executed search.",
        "",
        "> Unverified discovery leads only. Indexed page age is not a job-posting date. Check the employer page, stack, mobility, and local application history before recommending. Respect the latest handoff instruction on inbox access.",
        "",
    ]
    if report["status"] == "needs_credential":
        lines.extend(["Set `BRAVE_SEARCH_API_KEY` to execute these queries, or assign the generated query pack to a browsing agent.", ""])
    for item in report["results"]:
        lines.extend([f"- [{item['title']}]({item['url']}) ({', '.join(item['role_packs'])})", f"  {item['snippet']}"])
    if report["failures"]:
        lines.extend(["", "## Failures", ""])
        lines.extend(f"- {item['target']} / {item['role_pack']}: {item['error']}" for item in report["failures"])
    lines.extend(["", "## Search Stages", "", "Fallbacks run only after an empty accepted-result set, not after network/authentication failures. Undated fallback hits are not recent postings.", ""])
    for item in report.get("query_receipts", []):
        lines.append(f"- {item['target']} / {item['role_pack']} / {item['stage']}: {item['status']} ({item['accepted_results']} accepted results)")
    markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return markdown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", required=True)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--tier", type=int, choices=[1, 2], default=1)
    parser.add_argument("--expanded", action="store_true", help="Use one search per ATS instead of grouped hosts")
    parser.add_argument("--no-fallbacks", action="store_true", help="Disable bounded progressive broadening after empty queries")
    parser.add_argument("--query-cache", type=Path, help="Share same-day query receipts across one rotation; no extra API calls for identical fallbacks")
    parser.add_argument("--mode", choices=["lane", "title-xray"], default="lane")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between API calls")
    args = parser.parse_args()
    config = config_data(CONFIG_PATH.name, require_profile=True)
    if args.lane not in config["lane_filters"] or args.days < 1 or args.delay < 0:
        parser.error("Choose a configured lane, positive days, and a nonnegative delay")
    suffix = "-title-xray" if args.mode == "title-xray" else ""
    output = args.output or RUNS / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%SZ}-ats-web-{args.lane}{suffix}.json"
    if not output.is_absolute():
        output = ROOT / output
    cache = load(args.query_cache) if args.query_cache and args.query_cache.exists() else {}
    report = collect(config, args.lane, args.days, args.tier, os.environ.get("BRAVE_SEARCH_API_KEY", ""), delay=args.delay, expanded=args.expanded, mode=args.mode, fallbacks=not args.no_fallbacks, query_cache=cache)
    if args.query_cache:
        args.query_cache.parent.mkdir(parents=True, exist_ok=True)
        args.query_cache.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
    markdown = write_report(output, report)
    print(f"ATS web search: {report['status']} | Queries: {report['queries_succeeded']}/{report['queries_planned']} | Leads: {len(report['results'])} | Reused: {report['queries_reused']} | Skipped: {report['queries_skipped']}")
    print(f"Raw data: {output}")
    print(f"Report: {markdown}")
    return 0 if report["status"] in {"completed", "needs_credential", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
