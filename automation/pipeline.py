"""Shared normalization, state, and reporting for job discovery adapters."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from settings import config_data, term_matches
from typing import Any

from discover_jobs import (
    canonical_url,
    clean_description,
    fit_signal,
    known_in_handoff,
    load_json,
    mobility_signal,
    repair_text,
    route_kind,
    save_json,
)
from job_key import make_key
from job_history import reconcile_jobs, review_candidates
from application_checks import check_posting

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "search-config.json"
STATE_PATH = ROOT / ".local" / "state" / "seen_jobs.json"
RUNS_DIR = ROOT / ".local" / "state" / "runs"
HANDOFF_PATH = ROOT / ".local" / "handoff.md"


def config() -> dict[str, Any]:
    return config_data(CONFIG_PATH.name)


def annotate_job(job: dict[str, Any], search_config: dict[str, Any]) -> dict[str, Any]:
    guarded = check_posting(repair_text(job.get("description")))
    normalized = {
        **job,
        "title": repair_text(job.get("title")) or "(untitled)",
        "company": repair_text(job.get("company")) or "Unknown company",
        "location": repair_text(job.get("location")),
        "description": clean_description(guarded["text"]),
        "posting_integrity": guarded["integrity"],
        "url": canonical_url(repair_text(job.get("url"))),
        "skills": [repair_text(item).lower() for item in job.get("skills", [])],
        "countries": [repair_text(item).lower() for item in job.get("countries", [])],
        "regions": [repair_text(item).lower() for item in job.get("regions", [])],
    }
    fit, matched_terms, sort_score = fit_signal(normalized, search_config)
    normalized["fit_signal"] = fit
    normalized["matched_terms"] = matched_terms
    normalized["sort_score"] = sort_score
    normalized["mobility_signal"] = mobility_signal(normalized)
    normalized["route_kind"] = route_kind(normalized["url"], search_config)
    normalized["key"] = make_key(normalized["company"], normalized["title"], normalized["url"])
    return normalized


def relevant_role_title(title: str, search_config=None) -> bool:
    search_config = search_config if search_config is not None else config()
    if not title or any(term_matches(term, title) for term in search_config.get("excluded_titles", [])):
        return False
    return any(term_matches(term, title) for term in search_config.get("title_terms", []))


def keeps_low_fit_role(job: dict[str, Any]) -> bool:
    return relevant_role_title(job["title"])


def record_jobs(jobs: list[dict[str, Any]], source_group: str, *, persist: bool = True) -> dict[str, int]:
    state = load_json(STATE_PATH) if STATE_PATH.exists() else {"version": 1, "seen": {}}
    handoff = HANDOFF_PATH.read_text(encoding="utf-8").lower() if HANDOFF_PATH.exists() else ""
    counts = reconcile_jobs(state, jobs, source_group, lambda job: known_in_handoff(job, handoff),
                            datetime.now(timezone.utc).date().isoformat())
    if persist:
        save_json(STATE_PATH, state)
    return counts


def annotate_history(jobs: list[dict[str, Any]], source_group: str) -> dict[str, int]:
    return record_jobs(jobs, source_group, persist=False)


def write_discovery_report(
    prefix: str,
    lane: str,
    jobs: list[dict[str, Any]],
    failures: list[dict[str, str]],
    source_checks: list[dict[str, Any]] | None = None,
) -> tuple[Path, Path]:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%SZ")
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RUNS_DIR / f"{stamp}-{prefix}-{lane}.json"
    md_path = RUNS_DIR / f"{stamp}-{prefix}-{lane}.md"
    payload = {
        "meta": {
            "created_at": now.isoformat(),
            "source_group": prefix,
            "lane": lane,
            "results": len(jobs),
            "failures": failures,
            "source_checks": source_checks or [],
        },
        "results": jobs,
    }
    candidates = review_candidates(jobs)
    payload["meta"]["decision_counts"] = {
        "unreviewed": sum(job.get("review_status", "needs_review") == "needs_review" for job in candidates),
        "contact_first": sum(job.get("review_status") == "contact_first" for job in candidates),
        "applied_or_closed": sum(bool(job.get("application_status")) for job in jobs),
        "apply_ready_verified": sum(job.get("review_status") == "apply_ready" and bool(job.get("eligibility_verified"))
                                    and bool(job.get("eligibility_evidence")) for job in candidates),
    }
    save_json(json_path, payload)
    candidates.sort(key=lambda item: item["sort_score"], reverse=True)
    lines = [
        f"# {prefix.replace('-', ' ').title()} Discovery: {lane}",
        "",
        "> Leads only. Verify the direct vacancy, mobility, freshness, mandatory requirements, and local application history. Follow the latest handoff policy; do not check Gmail when prohibited.",
        "",
        f"Generated: `{now.isoformat()}` | Results: {len(jobs)} | Failures: {len(failures)}",
        f"Decision counts: {payload['meta']['decision_counts']}. Technical fit and mobility keywords are not verified eligibility.",
        f"Review queue: {len(candidates)} candidates; showing up to 60. Every row remains in the JSON and persistent queue.",
        "",
        "| Discovery / Review | Fit | Mobility signal | Company | Exact title | Location | Link |",
        "|---|---|---|---|---|---|---|",
    ]
    if source_checks:
        lines[5:5] = [
            f"Source coverage: {sum(item['status'] == 'completed' for item in source_checks)}/{len(source_checks)} boards completed. See raw JSON for per-board receipts.",
            "",
        ]
    for job in candidates[:60]:
        values = [
            f"{job['status']} / {job.get('review_status', 'needs_review')}",
            job["fit_signal"],
            job["mobility_signal"],
            job["company"],
            job["title"],
            job.get("location", ""),
        ]
        escaped = [str(value or "").replace("|", "\\|").replace("\n", " ") for value in values]
        lines.append(f"| {' | '.join(escaped)} | [open]({job['url']}) |")
    if not candidates:
        lines.append("| - | - | - | No review-ready leads from this pass | - | - | - |")
    if any(job.get("manual_fit_review") is True for job in candidates):
        lines.extend(["", "Low-fit rows were retained by a board-specific manual-review exception. Their fit and mobility are not verified or upgraded."])
    if failures:
        lines.extend(["", "## Source Failures", ""])
        lines.extend(f"- `{item['source']}`: {item['error']}" for item in failures)
    lines.extend(["", "Agents must write verified findings to their assigned result file, not directly into shared state.", ""])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, json_path
