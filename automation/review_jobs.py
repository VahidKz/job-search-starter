#!/usr/bin/env python3
"""Export the durable review backlog or explicitly record a local decision."""

from __future__ import annotations

import argparse
import copy
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from discover_jobs import load_json, save_json
from job_history import APPLICATION_ALIASES, REVIEW_STATUSES, queue_summary, reconcile_state, set_decision
from job_key import job_identity
from workflow_dates import parse_day, today_local

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local" / "state" / "seen_jobs.json"


def number(value) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else 0
    except (TypeError, ValueError):
        return 0


def next_batch(state: dict, *, limit: int = 10, statuses=None, focus: str = "",
               min_score: float | None = None, today=None) -> dict:
    """Project a bounded review batch without claiming old sightings are new/live."""
    if limit < 1:
        raise ValueError("limit must be positive")
    reconcile_state(state)
    today = today or today_local()
    statuses = set(statuses or ["needs_review"])
    candidates, expired, cooldown = [], [], []
    excluded = 0
    for identity, row in state["review_queue"].items():
        if identity in state["applications"]:
            excluded += 1
            continue
        if row.get("review_status") not in statuses:
            continue
        if focus.casefold() not in " ".join(str(row.get(k) or "") for k in
                                             ("company", "title", "location", "matched_terms")).casefold():
            continue
        score = row.get("technical_match_score")
        if min_score is not None and (score is None or number(score) < min_score):
            continue
        if min_score is None and row.get("fit_signal") == "low" and score is None:
            continue
        item = {key: row.get(key) for key in (
            "identity", "company", "title", "url", "location", "review_status", "first_seen", "last_seen",
            "posted_date", "deadline", "technical_match_score", "fit_signal", "sort_score", "mobility_signal",
            "strengths", "gaps", "active_status", "last_fetch_error", "fetch_attempts", "posting_integrity",
        )}
        deadline = parse_day(row.get("deadline"))
        item["deadline_warning"] = "unparseable" if row.get("deadline") and not deadline else None
        item["closing_soon"] = bool(deadline and 0 <= (deadline - today).days <= 7)
        if deadline and deadline < today:
            expired.append(item)
            continue
        retry = parse_day(row.get("retry_after"))
        if retry and retry > today:
            cooldown.append(item)
            continue
        candidates.append(item)
    candidates.sort(key=lambda r: (not r["closing_soon"],
                                    -number(r["technical_match_score"]), -number(r["sort_score"]),
                                    str(r["first_seen"] or ""), str(r["identity"])))
    return {"as_of": today.isoformat(), "eligible": len(candidates),
            "deferred": max(0, len(candidates) - limit), "excluded_by_application": excluded,
            "past_deadline": len(expired), "retry_cooldown": len(cooldown),
            "past_deadline_examples": expired[:limit], "retry_examples": cooldown[:limit],
            "note": "Review backlog, not new postings or verified eligibility. Unreachable is never automatically closed.",
            "results": candidates[:limit]}


def apply_assessments(state: dict, rows: list[dict]) -> dict:
    """Validate the entire batch before replacing any local review evidence."""
    if not isinstance(rows, list) or not rows:
        raise ValueError("Assessments must be a nonempty JSON array")
    updated = copy.deepcopy(state)
    reconcile_state(updated)
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("url"), str) or not row["url"].strip():
            raise ValueError("Every assessment needs an exact role URL")
        identity = job_identity(row)
        if identity in seen or identity not in updated["review_queue"]:
            raise ValueError("Duplicate or unknown assessment URL")
        if identity in updated["applications"]:
            raise ValueError("Do not assess a confirmed application as a new candidate")
        seen.add(identity)
        score = row.get("technical_match_score")
        if score is not None and (isinstance(score, bool) or not isinstance(score, (float, int))
                                  or not math.isfinite(score) or not 0 <= score <= 10):
            raise ValueError("Technical score must be a finite number from0 to10")
        for field in ("strengths", "gaps"):
            if field in row and (not isinstance(row[field], list)
                                 or any(not isinstance(v, str) for v in row[field])):
                raise ValueError(field + " must be a string array")
        status = row.get("review_status", "contact_first")
        active = row.get("active_status", "unverified")
        if active not in {"unverified", "listed_open", "closed", "unreachable"}:
            raise ValueError("Invalid active_status")
        if active == "unreachable" and status not in {"needs_review", "contact_first"}:
            raise ValueError("A failed fetch is not proof of closure or eligibility")
        if active == "closed" and status == "apply_ready":
            raise ValueError("Closed role cannot be apply-ready")
        set_decision(updated, row["url"], status, row.get("reason", ""),
                     eligibility_evidence=row.get("eligibility_evidence", ""))
        entry = updated["review_queue"][identity]
        for field in ("technical_match_score", "strengths", "gaps", "deadline", "resume", "active_status"):
            if field in row:
                entry[field] = row[field]
        if active == "unreachable":
            from datetime import timedelta
            entry.update(fetch_attempts=int(entry.get("fetch_attempts", 0)) + 1,
                         last_fetch_error=str(row.get("fetch_error", "Retrieval failed"))[:200],
                         retry_after=(today_local() + timedelta(days=2)).isoformat())
        elif active == "listed_open":
            entry.pop("retry_after", None)
            entry.pop("last_fetch_error", None)
    return updated


def snapshot(state: dict) -> dict:
    reconcile_state(state)
    rows = []
    for identity, row in state["review_queue"].items():
        if identity not in state["applications"] and row["review_status"] in {"needs_review", "contact_first", "apply_ready"}:
            rows.append(row)
    rows.sort(key=lambda row: (row.get("sort_score", 0) or 0, row.get("technical_match_score", 0) or 0), reverse=True)
    return {"created_at": datetime.now(timezone.utc).isoformat(), "counts": queue_summary(state),
            "note": "Unverified backlog, not fresh postings or proof of active/eligible jobs. No inbox or applications.",
            "results": rows}


def write_snapshot(output: Path, report: dict, limit: int = 100) -> None:
    save_json(output, report)
    lines = ["# Persistent Job Review Queue", "", report["note"], "",
             f"Decision counts: {report['counts']}",
             f"Showing {min(limit, len(report['results']))}/{len(report['results'])}; JSON retains every queued row.", "",
             "| Review | Company | Role | First Seen | Publication | Mobility Signal | Link |",
             "|---|---|---|---|---|---|---|"]
    for row in report["results"][:limit]:
        values = [row.get(key) or "unknown" for key in
                  ("review_status", "company", "title", "first_seen", "posted_date", "mobility_signal")]
        cells = [str(value).replace("|", "\\|").replace("\n", " ") for value in values]
        lines.append("| " + " | ".join(cells) + f" | [open]({row.get('url', '')}) |")
    output.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE)
    parser.add_argument("--output", type=Path, default=ROOT / ".local" / "state" / "review-queue.json")
    parser.add_argument("--refresh", action="store_true", help="Additively import legacy decisions; retain legacy records")
    parser.add_argument("--url")
    parser.add_argument("--next", action="store_true", help="Print a bounded JSON batch, not the full state")
    parser.add_argument("--batch-output", type=Path, help="Optional bounded batch file for --next")
    parser.add_argument("--focus", default="", help="Filter company/title/location/terms for --next")
    parser.add_argument("--status", action="append", choices=sorted(REVIEW_STATUSES))
    parser.add_argument("--min-score", type=float, help="Minimum manually reviewed technical score, 0-10")
    parser.add_argument("--assessments", type=Path, help="Validated batch of explicit manual assessments")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--set-review", choices=sorted(REVIEW_STATUSES))
    action.add_argument("--set-application", choices=sorted(APPLICATION_ALIASES))
    parser.add_argument("--reason", default="")
    parser.add_argument("--eligibility-evidence", default="")
    parser.add_argument("--limit", type=int, default=None, help="Batch size(default10) or snapshot preview(default100)")
    args = parser.parse_args()
    limit = args.limit if args.limit is not None else (10 if args.next else 100)
    if limit < 1:
        parser.error("limit must be positive")
    if args.min_score is not None and (not math.isfinite(args.min_score) or not 0 <= args.min_score <= 10):
        parser.error("min-score must be from0 to10")
    if args.next and (args.refresh or args.assessments or args.set_review or args.set_application):
        parser.error("--next is read-only; record decisions in a separate invocation")
    if args.assessments and (args.set_review or args.set_application):
        parser.error("Use either batch assessments or one explicit decision")
    if not args.next and (args.batch_output or args.focus or args.status or args.min_score is not None):
        parser.error("Batch filters/output require --next")
    state = load_json(args.state) if args.state.exists() else {"version": 1, "seen": {}}
    reconcile_state(state)
    if args.next:
        report = next_batch(state, limit=limit, statuses=args.status, focus=args.focus, min_score=args.min_score)
        if args.batch_output:
            if args.batch_output.resolve() in {args.state.resolve(), args.output.resolve()}:
                parser.error("Batch output must not replace state or the full review snapshot")
            save_json(args.batch_output, report)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0
    if args.assessments:
        try:
            state = apply_assessments(state, json_load_assessments(args.assessments))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(str(exc))
    if args.set_review or args.set_application:
        if not args.url:
            parser.error("A local decision requires --url")
        try:
            set_decision(state, args.url, args.set_review or args.set_application, args.reason,
                         application=bool(args.set_application), eligibility_evidence=args.eligibility_evidence)
        except ValueError as exc:
            parser.error(str(exc))
    if args.refresh or args.set_review or args.set_application or args.assessments:
        save_json(args.state, state)
    report = snapshot(state)
    write_snapshot(args.output, report, limit)
    print(f"Review decisions: {report['counts']}")
    print(f"Raw data: {args.output}")
    print(f"Report: {args.output.with_suffix('.md')}")
    return 0


def json_load_assessments(path: Path):
    import json
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
