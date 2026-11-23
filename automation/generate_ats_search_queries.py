#!/usr/bin/env python3
"""Generate lane-specific search-engine queries for jobs hosted on major ATS platforms."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from settings import config_data
from typing import Any
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "search-engine-ats.json"
RUNS = ROOT / ".local" / "state" / "runs"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def quoted(value: str) -> str:
    return f'"{value}"'


def or_group(values: list[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return quoted(values[0])
    return "(" + " OR ".join(quoted(value) for value in values) + ")"


def site_group(sites: list[str]) -> str:
    expressions = [f"site:{site}" for site in sites]
    if len(expressions) == 1:
        return expressions[0]
    return "(" + " OR ".join(expressions) + ")"


def search_targets(config: dict[str, Any], expanded: bool, mode: str = "lane") -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for group in config["platform_groups"]:
        if expanded or mode == "title-xray":
            for platform in group["platforms"]:
                if mode == "title-xray" and not platform.get("xray_sites"):
                    continue
                targets.append(
                    {
                        "name": platform["name"],
                        "group": group["name"],
                        "tier": group["tier"],
                        "cadence": group["cadence"],
                        "sites": platform["xray_sites"] if mode == "title-xray" else platform["sites"],
                    }
                )
        else:
            targets.append(
                {
                    "name": group["name"],
                    "group": group["name"],
                    "tier": group["tier"],
                    "cadence": group["cadence"],
                    "sites": [
                        site
                        for platform in group["platforms"]
                        for site in platform["sites"]
                    ],
                }
            )
    return targets


def build_queries(
    config: dict[str, Any],
    lane: str,
    days: int,
    today: date | None = None,
    expanded: bool = False,
    mode: str = "lane",
    stage: str = "targeted_recent",
) -> list[dict[str, Any]]:
    if mode not in {"lane", "title-xray"}:
        raise ValueError(f"Unknown query mode: {mode}")
    if lane not in config["lane_filters"]:
        raise KeyError(lane)
    if days < 1:
        raise ValueError("days must be positive")
    if stage not in {"targeted_recent", "stack_recent", "stack_anytime"}:
        raise ValueError("Unknown search relaxation stage")

    today = today or datetime.now().astimezone().date()
    cutoff = today - timedelta(days=days)
    # Title discovery is global; eligibility wording is checked on the original vacancy.
    filters = config["lane_filters"][lane] if mode == "lane" and stage == "targeted_recent" else {}
    packs = config["role_packs"] if mode == "lane" else config["title_packs"]
    queries: list[dict[str, Any]] = []
    for target in search_targets(config, expanded, mode):
        for pack in packs:
            parts = [site_group(target["sites"]), or_group(pack["terms"])]
            if filters.get("locations"):
                parts.append(or_group(filters["locations"]))
            if filters.get("mobility"):
                parts.append(or_group(filters["mobility"]))
            if stage != "stack_anytime":
                parts.append(f"after:{cutoff.isoformat()}")
            query = " ".join(part for part in parts if part)
            if len(query) > config["max_query_chars"]:
                raise ValueError(f"query exceeds {config['max_query_chars']} characters: {query}")
            word_count = len(query.split())
            if word_count > config["max_query_words"]:
                raise ValueError(f"query exceeds {config['max_query_words']} words: {query}")
            queries.append(
                {
                    "target": target["name"],
                    "group": target["group"],
                    "tier": target["tier"],
                    "cadence": target["cadence"],
                    "role_pack": pack["name"],
                    "word_count": word_count,
                    "query": query,
                    "sites": target["sites"],
                    "mode": mode,
                        "stage": stage,
                    "google_url": "https://www.google.com/search?" + urlencode({"q": query}),
                }
            )
    return queries


def build_query_plan(config: dict, lane: str, days: int, today: date | None = None,
                     expanded: bool = False, mode: str = "lane") -> list[dict]:
    unique = {}
    for stage in ("targeted_recent", "stack_recent", "stack_anytime"):
        for item in build_queries(config, lane, days, today, expanded, mode, stage):
            unique.setdefault(item["query"], item)
    return list(unique.values())


def render_markdown(lane: str, days: int, expanded: bool, queries: list[dict[str, Any]], mode: str = "lane") -> str:
    display_mode = "global title X-ray, per-platform" if mode == "title-xray" else ("expanded per-platform" if expanded else "compact grouped")
    lines = [
        f"# Search-Engine ATS Queries: {lane}",
        "",
        f"Window: last {days} day(s) | Mode: {display_mode} | Queries: {len(queries)}",
        "",
        "> Prepared queries, not executed searches. Search-index dates can be recrawl dates. Verify the original vacancy, date, active status, stack, sponsorship or candidate-location eligibility, and local application history. Do not access inboxes.",
        "",
        "Use the query as written in Google. In other search engines, apply their recent-date filter if `after:` is ignored.",
        "",
    ]
    if mode == "title-xray":
        lines.extend(["No country, stack, or mobility keyword is required by this pack. Route leads to the relevant country lane for verification; do not infer eligibility from a search hit.", ""])
    current: tuple[int, str] | None = None
    for item in queries:
        heading = (item["tier"], item["target"])
        if heading != current:
            lines.extend(
                [
                    f"## Tier {item['tier']}: {item['target']}",
                    "",
                    f"Cadence: `{item['cadence']}`",
                    "",
                ]
            )
            current = heading
        lines.extend([f"### {item['role_pack']} / {item.get('stage', 'targeted_recent')}", "", f"```text\n{item['query']}\n```", "", f"[Search Google]({item['google_url']})", ""])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", default="global", help="Search lane from search-engine-ats.json")
    parser.add_argument("--days", type=int, default=3, help="Recent-date window used for Google's after: operator")
    parser.add_argument("--expanded", action="store_true", help="Generate one query per ATS instead of grouped queries")
    parser.add_argument("--fallbacks", action="store_true", help="Include progressively relaxed and undated fallback queries; execute only after empty results")
    parser.add_argument("--mode", choices=["lane", "title-xray"], default="lane", help="Title X-ray searches all ten selected ATS platforms globally without mobility filters")
    parser.add_argument("--output", type=Path, help="Markdown output path")
    parser.add_argument("--list", action="store_true", help="List supported lanes and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = config_data(CONFIG_PATH.name, require_profile=True)
    if args.list:
        print("\n".join(config["lane_filters"]))
        return 0
    if args.lane not in config["lane_filters"]:
        print(f"Unknown lane {args.lane!r}. Available: {', '.join(config['lane_filters'])}", file=sys.stderr)
        return 2
    if args.days < 1:
        print("--days must be positive", file=sys.stderr)
        return 2

    builder = build_query_plan if args.fallbacks else build_queries
    queries = builder(config, args.lane, args.days, expanded=args.expanded, mode=args.mode)
    output = args.output
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
        mode = "title-xray" if args.mode == "title-xray" else ("expanded" if args.expanded else "grouped")
        output = RUNS / f"{stamp}-ats-search-{args.lane}-{mode}.md"
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_markdown(args.lane, args.days, args.expanded, queries, args.mode), encoding="utf-8")
    print(f"Queries: {len(queries)}")
    print(f"Report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
