"""Shared application ledger and durable review queue; discovery is not a decision."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Callable

from job_key import canonical_url, job_identity

APPLICATION_ALIASES = {
    "applied": "applied", "submitted": "applied", "applied_user_confirmed": "applied",
    "interviewing": "interviewing", "interview": "interviewing", "rejected": "rejected",
    "withdrawn": "withdrawn", "offer": "offer", "accepted": "accepted", "skipped": "skipped",
    "no_response": "no_response", "offer_declined": "offer_declined",
}
REVIEW_STATUSES = {"needs_review", "contact_first", "apply_ready", "reviewed", "excluded"}
DISCOVERY_STATUSES = {"", "discovered_unverified", "already_seen", "already_seen_cross_source", "known_in_handoff"}
QUEUE_FIELDS = (
    "company", "title", "url", "source_identity", "source_job_id", "requisition_id", "location",
    "posted_date", "updated_at", "fit_signal", "sort_score", "matched_terms", "mobility_signal",
    "source_observation", "active_status", "first_seen", "last_seen", "resume", "technical_match_score",
    "deadline", "strengths", "gaps", "posting_integrity",
)


def application_status(record: dict) -> str | None:
    return APPLICATION_ALIASES.get(record.get("application_status")) or APPLICATION_ALIASES.get(record.get("status"))


def review_status(record: dict) -> str:
    explicit = record.get("review_status")
    if explicit in REVIEW_STATUSES:
        return explicit
    status = str(record.get("status") or "")
    if "contact_first" in status or "enquiry" in status or "enquiries" in status:
        return "contact_first"
    if status in {"excluded", "closed", "duplicate"}:
        return "excluded"
    return "needs_review" if status in DISCOVERY_STATUSES else "reviewed"


def reconcile_state(state: dict) -> None:
    """Import legacy evidence additively, without changing either legacy mapping."""
    applications = state.setdefault("applications", {})
    queue = state.setdefault("review_queue", {})
    for group in ("source_seen", "seen"):
        for key, record in state.get(group, {}).items():
            if not isinstance(record, dict):
                continue
            identity = job_identity(record)
            if not identity:
                continue
            outcome = application_status(record)
            if outcome:
                entry = applications.setdefault(identity, {
                    "company": record.get("company"), "title": record.get("title"),
                    "url": canonical_url(record.get("url") or ""), "status": outcome,
                    "origin": "legacy_import", "evidence": [],
                })
                evidence = {"group": group, "key": key, "status": record.get("status")}
                if evidence not in entry.setdefault("evidence", []):
                    entry["evidence"].append(evidence)
                if entry["status"] != outcome:
                    entry["legacy_status_conflict"] = True
            entry = queue.setdefault(identity, {"identity": identity, "review_status": "needs_review"})
            for field in QUEUE_FIELDS:
                if field in {"first_seen", "last_seen"} and record.get(field):
                    values = [value for value in (entry.get(field), record[field]) if value]
                    entry[field] = (min if field == "first_seen" else max)(values)
                elif record.get(field) is not None and not entry.get(field):
                    entry[field] = record[field]
            decision = review_status(record)
            if entry["review_status"] == "needs_review" and not entry.get("reviewed_at") and decision != "needs_review":
                entry["review_status"] = decision
                entry["legacy_decision"] = record.get("status")
            entry.setdefault("eligibility_verified", False)


def review_candidates(jobs: list[dict]) -> list[dict]:
    return [job for job in jobs
            if not application_status(job)
            and review_status(job) in {"needs_review", "contact_first", "apply_ready"}
            and (job.get("fit_signal") in {"high", "medium"} or job.get("manual_fit_review") is True)
            and job.get("mobility_signal") != "likely_location_blocker"]


def reconcile_jobs(state: dict, jobs: list[dict], source: str,
                   in_handoff: Callable[[dict], bool], today: str) -> dict[str, int]:
    reconcile_state(state)
    seen = state.setdefault("seen", {})
    sources = state.setdefault("source_seen", {})
    queue, applications = state["review_queue"], state["applications"]
    known = {job_identity(record): key for key, record in seen.items() if isinstance(record, dict)}
    counts = {"new": 0, "known": 0, "seen": 0}
    for job in jobs:
        identity = job_identity(job)
        source_id = job.get("source_identity") or f"{source}:{identity}"
        if identity in applications:
            discovery = "known_application"
            counts["known"] += 1
        elif source_id in sources or identity in known:
            discovery = "already_seen"
            counts["seen"] += 1
        elif in_handoff(job):
            discovery = "known_in_handoff"
            counts["known"] += 1
        else:
            discovery = "discovered_unverified"
            counts["new"] += 1
        previous = sources.get(source_id, {})
        sources[source_id] = {**previous, "company": job["company"], "title": job["title"],
                              "url": job["url"], "first_seen": previous.get("first_seen", today),
                              "last_seen": today, "source": source, "discovery_status": discovery}
        entry = queue.setdefault(identity, {"identity": identity, "review_status": "needs_review",
                                            "first_seen": today, "eligibility_verified": False})
        for field in QUEUE_FIELDS:
            if field != "first_seen" and job.get(field) is not None:
                entry[field] = job[field]
        entry["last_seen"] = today
        entry.setdefault("first_seen", today)
        if entry["review_status"] == "apply_ready" and job.get("mobility_signal") in {"likely_location_blocker", "mobility_conflict"}:
            entry.update(review_status="contact_first", eligibility_verified=False,
                         previous_eligibility_evidence=entry.get("eligibility_evidence"), eligibility_evidence=None,
                         reason="New conflicting discovery evidence; recheck original eligibility")
        entry["discovery_status"] = discovery
        outcome = applications.get(identity, {}).get("status")
        job.update(discovery_status=discovery, review_status=entry["review_status"],
                   application_status=outcome, first_seen=entry["first_seen"], last_seen=today,
                   eligibility_verified=entry.get("eligibility_verified", False),
                   eligibility_evidence=entry.get("eligibility_evidence"),
                   status=outcome or discovery)
        record_key = known.get(identity) or job["key"]
        if record_key in seen and job_identity(seen[record_key]) != identity:
            record_key += "--" + hashlib.sha1(identity.encode()).hexdigest()[:8]
        existing = seen.get(record_key, {})
        seen[record_key] = {**existing, **{field: job.get(field) for field in QUEUE_FIELDS if field in job},
                            "first_seen": existing.get("first_seen", entry["first_seen"]),
                            "source": source, "status": existing.get("status") if application_status(existing) else outcome or existing.get("status", discovery),
                            "discovery_status": discovery, "review_status": entry["review_status"]}
        known[identity] = record_key
    return counts


def set_decision(state: dict, url: str, status: str, reason: str,
                 *, application: bool = False, eligibility_evidence: str = "") -> None:
    reconcile_state(state)
    identity = job_identity({"url": url})
    if identity not in state["review_queue"]:
        raise ValueError("URL is not in the local review queue")
    if not reason.strip():
        raise ValueError("A reason or user-confirmation note is required")
    entry = state["review_queue"][identity]
    stamp = datetime.now(timezone.utc).isoformat()
    if application:
        outcome = APPLICATION_ALIASES.get(status)
        if not outcome:
            raise ValueError("Unsupported application status")
        previous = state["applications"].get(identity, {})
        events = list(previous.get("events", []))
        if not events or (events[-1].get("status"), events[-1].get("reason")) != (outcome, reason):
            events.append({"status": outcome, "reason": reason, "recorded_at": stamp})
        state["applications"][identity] = {
            **previous,
            "company": entry.get("company"), "title": entry.get("title"), "url": entry["url"],
            "status": outcome, "reason": reason, "recorded_at": stamp, "origin": "explicit_local_update",
            "evidence": previous.get("evidence", []), "events": events,
        }
    else:
        if status not in REVIEW_STATUSES:
            raise ValueError("Unsupported review status")
        if status == "apply_ready" and not eligibility_evidence.strip():
            raise ValueError("Apply-ready requires role-specific eligibility evidence")
        entry.update(review_status=status, reason=reason, reviewed_at=stamp,
                     eligibility_verified=status == "apply_ready",
                     eligibility_checked_at=stamp if status == "apply_ready" else None,
                     eligibility_evidence=eligibility_evidence if status == "apply_ready" else None)


def queue_summary(state: dict) -> dict[str, int]:
    counts = {"needs_review": 0, "contact_first": 0, "apply_ready_verified": 0,
              "reviewed": 0, "excluded": 0, "applied_or_closed": 0}
    for identity, entry in state.get("review_queue", {}).items():
        if identity in state.get("applications", {}):
            counts["applied_or_closed"] += 1
        elif entry.get("review_status") == "apply_ready":
            key = "apply_ready_verified" if entry.get("eligibility_verified") and entry.get("eligibility_evidence") else "contact_first"
            counts[key] += 1
        else:
            counts[entry.get("review_status", "needs_review")] += 1
    return counts
